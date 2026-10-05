// LiveChat XR overlay layer: an implicit OpenXR API layer that draws your stream chat as a head-locked
// banner (VIEW-space quad) on top of OpenXR games.
//
// Config and IPC live in %LOCALAPPDATA%\LiveChatXR\:
//   config.ini  [games] exes=Game1.exe,Game2.exe   only these processes get the banner; all others pass through
//               [banner] seconds, up, distance, width (metres)
//   banner.txt  written by the LiveChat XR app; shown for `seconds` after each write
//   overlay.log this layer's log
// D3D11 sessions only. Any failure -> log, disable, pure pass-through for the rest of the process.
// Kill switch: environment variable LIVECHATXR_DISABLE=1 (declared in the JSON manifest).
//
// SPDX-License-Identifier: MIT
#define XR_USE_GRAPHICS_API_D3D11
#define XR_USE_PLATFORM_WIN32
#include <windows.h>
#include <d3d11.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <openxr/openxr_loader_negotiation.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

static const int W = 1024, H = 320;  // banner texture
static ULONGLONG g_showMs = 7000;

// Placement (metres, VIEW space); overridden by config.ini [banner].
static float g_up = 0.22f, g_dist = 1.0f, g_width = 0.62f;

static std::wstring g_dir;  // %LOCALAPPDATA%\LiveChatXR
static std::atomic<bool> g_active{false}, g_disabled{false};

static void Log(const char* fmt, ...) {
    if (g_dir.empty()) return;
    FILE* f = _wfopen((g_dir + L"\\overlay.log").c_str(), L"a");
    if (!f) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
    va_list a;
    va_start(a, fmt);
    vfprintf(f, fmt, a);
    va_end(a);
    fputc('\n', f);
    fclose(f);
}

static void Disable(const char* why) {
    if (!g_disabled.exchange(true)) Log("DISABLED: %s", why);
}

// ---------------------------------------------------------------- next-layer function table
static PFN_xrGetInstanceProcAddr nGIPA;
static XrInstance g_instance = XR_NULL_HANDLE;
#define NEXT_FNS(X) X(xrCreateSession) X(xrDestroySession) X(xrEndFrame) X(xrDestroyInstance) \
    X(xrCreateReferenceSpace) X(xrDestroySpace) X(xrCreateSwapchain) X(xrDestroySwapchain) \
    X(xrEnumerateSwapchainFormats) X(xrEnumerateSwapchainImages) X(xrAcquireSwapchainImage) \
    X(xrWaitSwapchainImage) X(xrReleaseSwapchainImage) X(xrGetSystemProperties)
#define DECL(f) static PFN_##f n_##f;
NEXT_FNS(DECL)

// ---------------------------------------------------------------- banner text -> pixels (worker thread)
static std::mutex g_mx;
static std::vector<uint32_t> g_pixels(W * H);  // grey + alpha, so RGBA and BGRA byte orders agree
static bool g_dirty = false;
static ULONGLONG g_until = 0;                  // GetTickCount64 deadline

static void Rasterize(const std::wstring& text, std::vector<uint32_t>& out) {
    BITMAPINFO bi{};
    bi.bmiHeader = {sizeof(BITMAPINFOHEADER), W, -H, 1, 32, BI_RGB};
    void* bits = nullptr;
    HDC dc = CreateCompatibleDC(nullptr);
    HBITMAP bmp = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    HGDIOBJ oldBmp = SelectObject(dc, bmp);
    HFONT font = CreateFontW(-50, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, 0, L"Segoe UI");
    HGDIOBJ oldFont = SelectObject(dc, font);
    memset(bits, 0, W * H * 4);
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, RGB(255, 255, 255));
    RECT r{28, 18, W - 28, H - 18};
    RECT calc = r;
    UINT flags = DT_WORDBREAK | DT_NOPREFIX | DT_EDITCONTROL | DT_END_ELLIPSIS;
    DrawTextW(dc, text.c_str(), -1, &calc, flags | DT_CALCRECT);
    DrawTextW(dc, text.c_str(), -1, &r, flags);
    GdiFlush();
    int boxBottom = (calc.bottom < H - 18 ? calc.bottom : H - 18) + 18;  // dark box hugs the text
    const uint8_t* src = (const uint8_t*)bits;
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x) {
            const uint8_t* p = src + (y * W + x) * 4;
            float c = (p[0] > p[1] ? (p[0] > p[2] ? p[0] : p[2]) : (p[1] > p[2] ? p[1] : p[2])) / 255.f;
            float bg = y < boxBottom ? 0.78f : 0.f;
            float a = c + bg * (1 - c);                 // text over dark box, straight alpha
            uint8_t v = a > 0 ? (uint8_t)(255 * c / a + .5f) : 0, A = (uint8_t)(255 * a + .5f);
            out[y * W + x] = (uint32_t)v | v << 8 | v << 16 | (uint32_t)A << 24;  // grey: RGBA == BGRA
        }
    SelectObject(dc, oldFont);
    DeleteObject(font);
    SelectObject(dc, oldBmp);
    DeleteObject(bmp);
    DeleteDC(dc);
}

static void Watcher() {
    std::wstring path = g_dir + L"\\banner.txt";
    FILETIME last{};
    std::vector<uint32_t> px(W * H);
    for (;;) {
        Sleep(200);
        WIN32_FILE_ATTRIBUTE_DATA fa;
        if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &fa)) continue;
        if (CompareFileTime(&fa.ftLastWriteTime, &last) == 0) continue;
        last = fa.ftLastWriteTime;
        FILE* f = _wfopen(path.c_str(), L"rb");
        if (!f) continue;
        std::string u8;
        char buf[4096];
        size_t n;
        while ((n = fread(buf, 1, sizeof buf, f)) > 0) u8.append(buf, n);
        fclose(f);
        if (u8.empty()) continue;
        std::wstring text(MultiByteToWideChar(CP_UTF8, 0, u8.data(), (int)u8.size(), nullptr, 0), L'\0');
        MultiByteToWideChar(CP_UTF8, 0, u8.data(), (int)u8.size(), text.data(), (int)text.size());
        FILETIME nowFt;
        GetSystemTimeAsFileTime(&nowFt);
        ULONGLONG now = ((ULONGLONG)nowFt.dwHighDateTime << 32) | nowFt.dwLowDateTime;
        ULONGLONG mt = ((ULONGLONG)last.dwHighDateTime << 32) | last.dwLowDateTime;
        ULONGLONG ageMs = now > mt ? (now - mt) / 10000 : 0;
        if (ageMs >= g_showMs) continue;  // stale file from before the game started
        Rasterize(text, px);
        std::lock_guard<std::mutex> l(g_mx);
        g_pixels.swap(px);
        g_dirty = true;
        g_until = GetTickCount64() + (g_showMs - ageMs);
    }
}

// ---------------------------------------------------------------- per-session overlay state
struct Overlay {
    XrSession session = XR_NULL_HANDLE;
    ID3D11Device* dev = nullptr;
    XrSpace view = XR_NULL_HANDLE;
    XrSwapchain sc = XR_NULL_HANDLE;
    std::vector<XrSwapchainImageD3D11KHR> imgs;
    uint32_t maxLayers = 16;
    bool hasImage = false;
} S;

static void Teardown() {
    if (S.sc) n_xrDestroySwapchain(S.sc);
    if (S.view) n_xrDestroySpace(S.view);
    S = Overlay{};
}

static bool Setup(XrSession session, XrSystemId sys, ID3D11Device* dev) {
    S.session = session;
    S.dev = dev;
    XrSystemProperties sp{XR_TYPE_SYSTEM_PROPERTIES};
    if (XR_SUCCEEDED(n_xrGetSystemProperties(g_instance, sys, &sp))) S.maxLayers = sp.graphicsProperties.maxLayerCount;

    XrReferenceSpaceCreateInfo rs{XR_TYPE_REFERENCE_SPACE_CREATE_INFO, nullptr, XR_REFERENCE_SPACE_TYPE_VIEW, {{0, 0, 0, 1}, {0, 0, 0}}};
    if (XR_FAILED(n_xrCreateReferenceSpace(session, &rs, &S.view))) return false;

    uint32_t n = 0;
    n_xrEnumerateSwapchainFormats(session, 0, &n, nullptr);
    std::vector<int64_t> fmts(n);
    n_xrEnumerateSwapchainFormats(session, n, &n, fmts.data());
    int64_t want[] = {DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM};
    int64_t fmt = 0;
    for (int64_t w : want) {
        for (int64_t f : fmts) if (f == w) { fmt = f; break; }
        if (fmt) break;
    }
    if (!fmt) return false;

    XrSwapchainCreateInfo ci{XR_TYPE_SWAPCHAIN_CREATE_INFO};
    ci.usageFlags = XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT | XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT | XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
    ci.format = fmt;
    ci.sampleCount = 1;
    ci.width = W;
    ci.height = H;
    ci.faceCount = 1;
    ci.arraySize = 1;
    ci.mipCount = 1;
    if (XR_FAILED(n_xrCreateSwapchain(session, &ci, &S.sc))) return false;
    n_xrEnumerateSwapchainImages(S.sc, 0, &n, nullptr);
    S.imgs.assign(n, {XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR});
    if (XR_FAILED(n_xrEnumerateSwapchainImages(S.sc, n, &n, (XrSwapchainImageBaseHeader*)S.imgs.data()))) return false;
    Log("session ready: format %lld, %u images, maxLayers %u", (long long)fmt, n, S.maxLayers);
    return true;
}

static bool Upload() {  // render thread only
    uint32_t idx;
    XrSwapchainImageAcquireInfo ai{XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO};
    if (XR_FAILED(n_xrAcquireSwapchainImage(S.sc, &ai, &idx))) return false;
    XrSwapchainImageWaitInfo wi{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO, nullptr, 100000000};  // 100 ms
    if (XR_FAILED(n_xrWaitSwapchainImage(S.sc, &wi))) return false;
    ID3D11DeviceContext* ctx = nullptr;
    S.dev->GetImmediateContext(&ctx);
    {
        std::lock_guard<std::mutex> l(g_mx);
        ctx->UpdateSubresource(S.imgs[idx].texture, 0, nullptr, g_pixels.data(), W * 4, 0);
        g_dirty = false;
    }
    ctx->Release();
    XrSwapchainImageReleaseInfo ri{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};
    if (XR_FAILED(n_xrReleaseSwapchainImage(S.sc, &ri))) return false;
    S.hasImage = true;
    return true;
}

// ---------------------------------------------------------------- hooks
static XrResult XRAPI_CALL h_xrCreateSession(XrInstance inst, const XrSessionCreateInfo* ci, XrSession* out) {
    XrResult r = n_xrCreateSession(inst, ci, out);
    if (XR_FAILED(r) || !g_active || g_disabled) return r;
    try {
        ID3D11Device* dev = nullptr;
        for (auto* h = (const XrBaseInStructure*)ci->next; h; h = h->next)
            if (h->type == XR_TYPE_GRAPHICS_BINDING_D3D11_KHR) dev = ((const XrGraphicsBindingD3D11KHR*)h)->device;
        if (!dev) { Disable("not a D3D11 session"); return r; }
        Teardown();
        if (!Setup(*out, ci->systemId, dev)) { Teardown(); Disable("session setup failed"); }
    } catch (...) { Disable("exception in xrCreateSession"); }
    return r;
}

static XrResult XRAPI_CALL h_xrDestroySession(XrSession s) {
    try { if (s == S.session) Teardown(); } catch (...) { Disable("exception in xrDestroySession"); }
    return n_xrDestroySession(s);
}

static XrResult XRAPI_CALL h_xrEndFrame(XrSession s, const XrFrameEndInfo* info) {
    if (g_disabled || s != S.session || !S.sc || !info) return n_xrEndFrame(s, info);
    try {
        bool dirty, visible;
        {
            std::lock_guard<std::mutex> l(g_mx);
            dirty = g_dirty;
            visible = GetTickCount64() < g_until;
        }
        if (dirty && !Upload()) { Disable("swapchain upload failed"); return n_xrEndFrame(s, info); }
        if (!visible || !S.hasImage || info->layerCount + 1 > S.maxLayers) return n_xrEndFrame(s, info);

        XrCompositionLayerQuad q{XR_TYPE_COMPOSITION_LAYER_QUAD};
        q.layerFlags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT | XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT;
        q.space = S.view;
        q.eyeVisibility = XR_EYE_VISIBILITY_BOTH;
        q.subImage = {S.sc, {{0, 0}, {W, H}}, 0};
        q.pose = {{0, 0, 0, 1}, {0, g_up, -g_dist}};
        q.size = {g_width, g_width * H / W};
        std::vector<const XrCompositionLayerBaseHeader*> layers(info->layers, info->layers + info->layerCount);
        layers.push_back((const XrCompositionLayerBaseHeader*)&q);
        XrFrameEndInfo mine = *info;
        mine.layerCount = (uint32_t)layers.size();
        mine.layers = layers.data();
        XrResult r = n_xrEndFrame(s, &mine);
        if (XR_SUCCEEDED(r)) return r;
        Log("xrEndFrame with banner returned %d", (int)r);
        Disable("runtime rejected the banner layer");
    } catch (...) { Disable("exception in xrEndFrame"); }
    return n_xrEndFrame(s, info);
}

static XrResult XRAPI_CALL h_xrDestroyInstance(XrInstance inst) {
    try { Teardown(); } catch (...) {}
    XrResult r = n_xrDestroyInstance(inst);
    if (inst == g_instance) g_instance = XR_NULL_HANDLE;
    return r;
}

static XrResult XRAPI_CALL h_xrGetInstanceProcAddr(XrInstance inst, const char* name, PFN_xrVoidFunction* fn) {
    if (!nGIPA) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult r = nGIPA(inst, name, fn);
    if (XR_FAILED(r) || !g_active) return r;
#define HOOK(f) if (!strcmp(name, #f)) { *fn = (PFN_xrVoidFunction)h_##f; return r; }
    HOOK(xrCreateSession) HOOK(xrDestroySession) HOOK(xrEndFrame) HOOK(xrDestroyInstance)
    HOOK(xrGetInstanceProcAddr)
    return r;
}

static XrResult XRAPI_CALL h_CreateApiLayerInstance(const XrInstanceCreateInfo* info, const XrApiLayerCreateInfo* li, XrInstance* inst) {
    XrApiLayerCreateInfo next = *li;
    next.nextInfo = li->nextInfo->next;
    XrResult r = li->nextInfo->nextCreateApiLayerInstance(info, &next, inst);
    if (XR_FAILED(r)) return r;
    nGIPA = li->nextInfo->nextGetInstanceProcAddr;
    g_instance = *inst;
    bool ok = true;
#define LOAD(f) ok &= XR_SUCCEEDED(nGIPA(*inst, #f, (PFN_xrVoidFunction*)&n_##f)) && n_##f;
    NEXT_FNS(LOAD)
    if (!ok && g_active) Disable("missing runtime functions");
    return r;
}

// Self-test hook for tests/test_layer.py: rasterize UTF-8 text into a W*H RGBA buffer.
extern "C" __declspec(dllexport) void livechatxr_render_test(const char* u8, uint32_t* out) {
    std::wstring t(MultiByteToWideChar(CP_UTF8, 0, u8, -1, nullptr, 0), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, u8, -1, t.data(), (int)t.size());
    std::vector<uint32_t> px(W * H);
    Rasterize(t.c_str(), px);
    memcpy(out, px.data(), px.size() * 4);
}

extern "C" __declspec(dllexport) XrResult XRAPI_CALL xrNegotiateLoaderApiLayerInterface(
    const XrNegotiateLoaderInfo* li, const char* /*layerName*/, XrNegotiateApiLayerRequest* req) {
    if (!li || !req || li->structType != XR_LOADER_INTERFACE_STRUCT_LOADER_INFO ||
        req->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST ||
        li->minInterfaceVersion > XR_CURRENT_LOADER_API_LAYER_VERSION ||
        li->maxInterfaceVersion < XR_CURRENT_LOADER_API_LAYER_VERSION)
        return XR_ERROR_INITIALIZATION_FAILED;
    req->layerInterfaceVersion = XR_CURRENT_LOADER_API_LAYER_VERSION;
    req->layerApiVersion = XR_CURRENT_API_VERSION;
    req->getInstanceProcAddr = h_xrGetInstanceProcAddr;
    req->createApiLayerInstance = h_CreateApiLayerInstance;

    static std::once_flag once;
    std::call_once(once, [] {
        wchar_t exe[MAX_PATH], la[MAX_PATH];
        GetModuleFileNameW(nullptr, exe, MAX_PATH);
        const wchar_t* base = wcsrchr(exe, L'\\');
        base = base ? base + 1 : exe;
        if (!GetEnvironmentVariableW(L"LOCALAPPDATA", la, MAX_PATH)) return;
        std::wstring dir = std::wstring(la) + L"\\LiveChatXR", ini = dir + L"\\config.ini";
        wchar_t games[2048];
        GetPrivateProfileStringW(L"games", L"exes", L"", games, 2048, ini.c_str());
        bool listed = false;
        for (wchar_t *ctx = nullptr, *t = wcstok_s(games, L",;", &ctx); t; t = wcstok_s(nullptr, L",;", &ctx)) {
            while (*t == L' ') ++t;
            size_t n = wcslen(t);
            while (n && t[n - 1] == L' ') t[--n] = 0;
            if (n && !_wcsicmp(t, base)) listed = true;
        }
        if (!listed) return;  // not a chosen game: never touch it
        g_dir = dir;
        WIN32_FILE_ATTRIBUTE_DATA fa;  // keep the log small
        if (GetFileAttributesExW((g_dir + L"\\overlay.log").c_str(), GetFileExInfoStandard, &fa) && fa.nFileSizeLow > (1u << 20))
            DeleteFileW((g_dir + L"\\overlay.log").c_str());
        wchar_t v[32];
        if (GetPrivateProfileStringW(L"banner", L"up", L"", v, 32, ini.c_str())) g_up = (float)_wtof(v);
        if (GetPrivateProfileStringW(L"banner", L"distance", L"", v, 32, ini.c_str())) g_dist = (float)_wtof(v);
        if (GetPrivateProfileStringW(L"banner", L"width", L"", v, 32, ini.c_str())) g_width = (float)_wtof(v);
        if (GetPrivateProfileStringW(L"banner", L"seconds", L"", v, 32, ini.c_str()) && _wtof(v) > 0) g_showMs = (ULONGLONG)(_wtof(v) * 1000);
        Log("negotiated in %ls (up %.2f, distance %.2f, width %.2f, %llu ms)", base, g_up, g_dist, g_width, g_showMs);
        g_active = true;
        std::thread(Watcher).detach();
    });
    return XR_SUCCESS;
}