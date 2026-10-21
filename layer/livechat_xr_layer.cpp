// LiveChat XR overlay layer: an implicit OpenXR API layer that draws your stream chat as a banner in front of you
// on top of OpenXR games. The banner is anchored in the world (LOCAL space) and lazily follows your head, like Quest
// menus: world-anchored layers survive the runtime's reprojection when the game drops frames, while a VIEW-locked
// quad judders against your head every time it does.
//
// Config and IPC live in %LOCALAPPDATA%\LiveChatXR\:
//   config.ini  [games] exes=Game1.exe,Game2.exe   only these processes get the banner; all others pass through
//               [banner] seconds, up, distance, width (metres), follow (degrees you can look away before the
//               banner glides back in front of you; 0 = locked to your view every frame, the old behaviour)
//   banner.txt  written by the LiveChat XR app; shown for `seconds` after each write
//   viewers.txt "tiktok 4\ntwitch 1" live viewer counts, drawn as a small top-right box with platform logos;
//               the app rewrites it every 5 s, so it hides 20 s after the app stops (or at once when empty)
//               [viewers] up, right (box's right edge), distance, width (metres)
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
#include <cmath>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

static const int W = 1024, H = 320;  // banner texture
static const int VW = 1024, VH = 96;  // viewer-count texture
static ULONGLONG g_showMs = 7000;

// Placement (metres, VIEW space); overridden by config.ini [banner] / [viewers].
static float g_up = 0.22f, g_dist = 1.0f, g_width = 0.62f;
static float g_vUp = 0.36f, g_vRight = 0.45f, g_vDist = 1.0f, g_vWidth = 0.44f;
static float g_followDeg = 20.f;  // config.ini [banner] follow; 0 = VIEW-locked

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

// config.ini values: out-of-range or non-finite (inf/nan parse via _wtof) keep the default instead of a broken quad.
static float Clamp(float v, float lo, float hi, float fallback) {
    return std::isfinite(v) ? (v < lo ? lo : v > hi ? hi : v) : fallback;
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
    X(xrWaitSwapchainImage) X(xrReleaseSwapchainImage) X(xrGetSystemProperties) X(xrLocateSpace)
#define DECL(f) static PFN_##f n_##f;
NEXT_FNS(DECL)

// ---------------------------------------------------------------- banner text -> pixels (worker thread)
static std::mutex g_mx;
static bool g_bgra = false;
struct Pane {  // one overlay quad: pixels from a watcher thread + its swapchain in the session
    int w, h;
    std::vector<uint32_t> pixels;  // RGBA (R in the low byte); swizzled at upload for BGRA swapchains
    bool dirty = false;
    ULONGLONG until = 0;           // GetTickCount64 deadline
    XrSwapchain sc = XR_NULL_HANDLE;
    std::vector<XrSwapchainImageD3D11KHR> imgs;
    bool hasImage = false;
    Pane(int w_, int h_) : w(w_), h(h_), pixels(w_ * h_) {}
};
static Pane g_banner(W, H), g_viewers(VW, VH);

// Lines starting with the gift emoji (U+1F381), optionally after a platform emoji, are drawn gold; everything else white.
static const wchar_t GIFT[] = L"\xD83C\xDF81";
static const COLORREF WHITE = RGB(255, 255, 255), GOLD = RGB(255, 196, 64);

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
    SetTextColor(dc, WHITE);  // coverage only; colour is applied per row below
    std::vector<COLORREF> rowColor(H, WHITE);
    const int pad = 18, bottom = H - pad;
    const UINT flags = DT_WORDBREAK | DT_NOPREFIX | DT_EDITCONTROL | DT_END_ELLIPSIS;
    int y = pad;
    for (size_t start = 0; start <= text.size() && y < bottom;) {
        size_t end = text.find(L'\n', start);
        if (end == std::wstring::npos) end = text.size();
        std::wstring line = text.substr(start, end - start);
        start = end + 1;
        if (line.empty()) continue;
        RECT r{28, y, W - 28, bottom};
        RECT calc = r;
        DrawTextW(dc, line.c_str(), -1, &calc, flags | DT_CALCRECT);
        DrawTextW(dc, line.c_str(), -1, &r, flags);
        // a platform emoji plus space (chat.ICONS) is 3 UTF-16 units, so a gift may start at 0 or 3
        COLORREF c = line.compare(0, 2, GIFT) == 0 || (line.size() >= 5 && line.compare(3, 2, GIFT) == 0) ? GOLD : WHITE;
        for (int row = y; row < calc.bottom && row < H; ++row) rowColor[row] = c;
        y = calc.bottom;
    }
    GdiFlush();
    int boxBottom = (y < bottom ? y : bottom) + pad;  // dark box hugs the text
    const uint8_t* src = (const uint8_t*)bits;
    for (int row = 0; row < H; ++row) {
        COLORREF col = rowColor[row];
        for (int x = 0; x < W; ++x) {
            const uint8_t* p = src + (row * W + x) * 4;
            float c = (p[0] > p[1] ? (p[0] > p[2] ? p[0] : p[2]) : (p[1] > p[2] ? p[1] : p[2])) / 255.f;
            float bg = row < boxBottom ? 0.78f : 0.f;
            float a = c + bg * (1 - c);  // text over dark box, straight alpha
            float k = a > 0 ? c / a : 0;
            uint32_t R = (uint32_t)(GetRValue(col) * k + .5f), G = (uint32_t)(GetGValue(col) * k + .5f),
                     B = (uint32_t)(GetBValue(col) * k + .5f), A = (uint32_t)(255 * a + .5f);
            out[row * W + x] = R | G << 8 | B << 16 | A << 24;
        }
    }
    SelectObject(dc, oldFont);
    DeleteObject(font);
    SelectObject(dc, oldBmp);
    DeleteObject(bmp);
    DeleteDC(dc);
}
// Viewer box: "N watching", then a logo + count per platform; right-aligned so it sits in the corner, with a dark
// rounded box hugging the content. Drawn at 2x and box-filtered down so the GDI shapes come out antialiased.
static int LogoWidth(const std::wstring& p, int s) { return p == L"youtube" ? s * 10 / 7 : s; }

static void Logo(HDC dc, const std::wstring& p, int x, int y, int s, HFONT note) {
    auto fill = [&](COLORREF c) {
        SelectObject(dc, GetStockObject(DC_BRUSH));
        SetDCBrushColor(dc, c);
        SelectObject(dc, GetStockObject(NULL_PEN));
    };
    if (p == L"youtube") {  // red rounded rectangle, white play triangle
        fill(RGB(255, 0, 0));
        RoundRect(dc, x, y + s / 8, x + s * 10 / 7, y + s * 7 / 8, s / 3, s / 3);
        fill(RGB(255, 255, 255));
        POINT t[] = {{x + s * 4 / 7, y + s * 5 / 16}, {x + s * 4 / 7, y + s * 11 / 16}, {x + s * 13 / 14, y + s / 2}};
        Polygon(dc, t, 3);
    } else if (p == L"twitch") {  // purple speech bubble with two white bars
        fill(RGB(145, 70, 255));
        Rectangle(dc, x + s / 8, y, x + s, y + s * 3 / 4);
        POINT t[] = {{x + s / 4, y + s * 3 / 4 - 1}, {x + s / 2, y + s * 3 / 4 - 1}, {x + s / 4, y + s}};
        Polygon(dc, t, 3);
        fill(RGB(255, 255, 255));
        Rectangle(dc, x + s * 7 / 16, y + s / 6, x + s * 9 / 16, y + s * 7 / 16);
        Rectangle(dc, x + s * 11 / 16, y + s / 6, x + s * 13 / 16, y + s * 7 / 16);
    } else if (p == L"tiktok") {  // TikTok's look: cyan and pink offset copies of a note under a white one
        HGDIOBJ old = SelectObject(dc, note);
        int o = s / 14;
        COLORREF cs[] = {RGB(37, 244, 238), RGB(254, 44, 85), RGB(255, 255, 255)};
        int d[] = {-o, o, 0};
        for (int i = 0; i < 3; ++i) {
            RECT q{x + d[i], y + d[i], x + s + d[i], y + s + d[i]};
            SetTextColor(dc, cs[i]);
            DrawTextW(dc, L"\x266A", 1, &q, DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
        }
        SelectObject(dc, old);
    }
}

static void RasterizeViewers(const std::wstring& text, std::vector<uint32_t>& out) {
    struct Item { std::wstring platform, count; };
    std::vector<Item> items;
    long total = 0;
    for (size_t start = 0; start < text.size();) {
        size_t end = text.find(L'\n', start);
        if (end == std::wstring::npos) end = text.size();
        std::wstring line = text.substr(start, end - start);
        start = end + 1;
        size_t sp = line.find(L' ');
        if (sp == std::wstring::npos || items.size() >= 8) continue;  // three platforms; never an unbounded list
        std::wstring count = line.substr(sp + 1);
        while (!count.empty() && (count.back() == L'\r' || count.back() == L' ')) count.pop_back();
        items.push_back({line.substr(0, sp), count});
        total += _wtol(count.c_str());
    }
    std::fill(out.begin(), out.end(), 0u);
    if (items.empty()) return;

    const int S = 2, w = VW * S, h = VH * S;  // supersampled canvas
    BITMAPINFO bi{};
    bi.bmiHeader = {sizeof(BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB};
    void* bits = nullptr;
    HDC dc = CreateCompatibleDC(nullptr);
    HBITMAP bmp = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    HGDIOBJ oldBmp = SelectObject(dc, bmp);
    HFONT font = CreateFontW(-46 * S, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, 0, L"Segoe UI");
    HFONT note = CreateFontW(-60 * S, 0, 0, 0, FW_BOLD, 0, 0, 0, DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, 0, L"Segoe UI Symbol");
    HGDIOBJ oldFont = SelectObject(dc, font);
    SetBkMode(dc, TRANSPARENT);
    const int pad = 18 * S, logo = 44 * S, gap = 8 * S, sep = 24 * S;
    auto width = [&](const std::wstring& s) { SIZE z{}; GetTextExtentPoint32W(dc, s.c_str(), (int)s.size(), &z); return (int)z.cx; };
    std::wstring head = std::to_wstring(total) + L" watching";
    int content = width(head);
    for (auto& it : items) content += sep + LogoWidth(it.platform, logo) + gap + width(it.count);
    int boxL = w - (content + 2 * pad < w ? content + 2 * pad : w);
    memset(bits, 0, (size_t)w * h * 4);
    const COLORREF BOX = RGB(24, 24, 24);  // non-zero, so "inside the box" is any non-black pixel
    SelectObject(dc, GetStockObject(DC_BRUSH));
    SetDCBrushColor(dc, BOX);
    SelectObject(dc, GetStockObject(NULL_PEN));
    RoundRect(dc, boxL, 0, w + 1, h + 1, 28 * S, 28 * S);
    TEXTMETRICW tm;
    GetTextMetricsW(dc, &tm);
    int x = boxL + pad, mid = h / 2, ty = mid - tm.tmHeight / 2;
    SetTextColor(dc, RGB(255, 255, 255));
    TextOutW(dc, x, ty, head.c_str(), (int)head.size());
    x += width(head);
    for (auto& it : items) {
        x += sep;
        Logo(dc, it.platform, x, mid - logo / 2, logo, note);
        x += LogoWidth(it.platform, logo) + gap;
        SetTextColor(dc, RGB(255, 255, 255));
        TextOutW(dc, x, ty, it.count.c_str(), (int)it.count.size());
        x += width(it.count);
    }
    GdiFlush();
    // Average each 2x2 block; pixels outside the box are transparent and don't darken the edge colour.
    const uint8_t* src = (const uint8_t*)bits;
    for (int y = 0; y < VH; ++y)
        for (int xx = 0; xx < VW; ++xx) {
            uint32_t r = 0, g = 0, b = 0, in = 0;
            for (int dy = 0; dy < S; ++dy)
                for (int dx = 0; dx < S; ++dx) {
                    const uint8_t* p = src + ((size_t)(y * S + dy) * w + xx * S + dx) * 4;  // BGRx
                    if (p[0] | p[1] | p[2]) { b += p[0]; g += p[1]; r += p[2]; ++in; }
                }
            out[y * VW + xx] = in ? (r / in) | (g / in) << 8 | (b / in) << 16 | (235 * in / (S * S)) << 24 : 0;
        }
    SelectObject(dc, oldFont);
    DeleteObject(font);
    DeleteObject(note);
    SelectObject(dc, oldBmp);
    DeleteObject(bmp);
    DeleteDC(dc);
}

// banner.txt / viewers.txt are a few hundred bytes; anything bigger is cut, never read whole into a game process.
static const size_t MAX_FILE = 64 * 1024;
static bool ReadCapped(const std::wstring& path, std::string& out) {
    FILE* f = _wfopen(path.c_str(), L"rb");
    if (!f) return false;
    out.resize(MAX_FILE);
    out.resize(fread(&out[0], 1, MAX_FILE, f));
    fclose(f);
    return true;
}

// Watches one file; each new write is rasterized and shown for showMs after its write time. Empty file = hide.
static void Watcher(Pane* pane, const wchar_t* name, ULONGLONG showMs,
                    void (*raster)(const std::wstring&, std::vector<uint32_t>&)) {
    std::wstring path = g_dir + L"\\" + name;
    FILETIME last{};
    std::vector<uint32_t> px(pane->w * pane->h);
    for (;;) try {
        Sleep(200);
        WIN32_FILE_ATTRIBUTE_DATA fa;
        if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &fa)) continue;
        if (CompareFileTime(&fa.ftLastWriteTime, &last) == 0) continue;
        last = fa.ftLastWriteTime;
        std::string u8;
        if (!ReadCapped(path, u8)) continue;
        if (u8.empty()) {
            std::lock_guard<std::mutex> l(g_mx);
            pane->until = 0;
            continue;
        }
        std::wstring text(MultiByteToWideChar(CP_UTF8, 0, u8.data(), (int)u8.size(), nullptr, 0), L'\0');
        MultiByteToWideChar(CP_UTF8, 0, u8.data(), (int)u8.size(), text.data(), (int)text.size());
        FILETIME nowFt;
        GetSystemTimeAsFileTime(&nowFt);
        ULONGLONG now = ((ULONGLONG)nowFt.dwHighDateTime << 32) | nowFt.dwLowDateTime;
        ULONGLONG mt = ((ULONGLONG)last.dwHighDateTime << 32) | last.dwLowDateTime;
        ULONGLONG ageMs = now > mt ? (now - mt) / 10000 : 0;
        if (ageMs >= showMs) continue;  // stale file from before the game started
        raster(text, px);
        std::lock_guard<std::mutex> l(g_mx);
        pane->pixels.swap(px);
        pane->dirty = true;
        pane->until = GetTickCount64() + (showMs - ageMs);
    } catch (...) {
        Log("watcher error; skipped one update");  // an escaped exception would std::terminate the game
    }
}

// ---------------------------------------------------------------- lazy follow (render thread only)
// The anchor is a gravity-aligned yaw/pitch plus a head position in LOCAL space. It stays put while you look within
// `follow` degrees of it, then glides back to your view (time constant TAU) until it is within SETTLE_DEG of it.
struct Anchor { float yaw = 0, pitch = 0, x = 0, y = 0, z = 0; bool valid = false, moving = false; };
static const float TAU = 0.2f, SETTLE_DEG = 2.f, MOVE_M = 0.3f, PI = 3.14159265f;

static float Wrap(float a) { while (a > PI) a -= 2 * PI; while (a < -PI) a += 2 * PI; return a; }

static void HeadAngles(const XrQuaternionf& q, float& yaw, float& pitch) {  // forward = q * (0, 0, -1)
    float fx = -2 * (q.x * q.z + q.w * q.y), fy = -2 * (q.y * q.z - q.w * q.x), fz = -(1 - 2 * (q.x * q.x + q.y * q.y));
    yaw = atan2f(-fx, -fz);
    pitch = asinf(fy < -1 ? -1 : fy > 1 ? 1 : fy);
}

static float AngleDeg(float y1, float p1, float y2, float p2) {  // between two forward directions
    float d = cosf(p1) * cosf(p2) * cosf(y1 - y2) + sinf(p1) * sinf(p2);
    return acosf(d < -1 ? -1 : d > 1 ? 1 : d) * 180 / PI;
}

static void Step(Anchor& a, float yaw, float pitch, float x, float y, float z, float dt, bool snap) {
    if (snap || !a.valid) { a = {yaw, pitch, x, y, z, true, false}; return; }
    float err = AngleDeg(a.yaw, a.pitch, yaw, pitch);
    float dx = x - a.x, dy = y - a.y, dz = z - a.z, dist = sqrtf(dx * dx + dy * dy + dz * dz);
    if (err > g_followDeg || dist > MOVE_M) a.moving = true;
    if (!a.moving) return;
    float k = 1 - expf(-(dt > 0 ? dt : 0) / TAU);
    a.yaw = Wrap(a.yaw + Wrap(yaw - a.yaw) * k);
    a.pitch += (pitch - a.pitch) * k;
    a.x += dx * k; a.y += dy * k; a.z += dz * k;
    float ex = x - a.x, ey = y - a.y, ez = z - a.z;
    if (AngleDeg(a.yaw, a.pitch, yaw, pitch) < SETTLE_DEG && sqrtf(ex * ex + ey * ey + ez * ez) < 0.02f) a.moving = false;
}

static XrPosef Place(const Anchor& a, float ox, float oy, float od) {  // offset (right, up, forward) from the anchor
    float cy = cosf(a.yaw / 2), sy = sinf(a.yaw / 2), cp = cosf(a.pitch / 2), sp = sinf(a.pitch / 2);
    XrQuaternionf q{cy * sp, sy * cp, -sy * sp, cy * cp};  // yaw about +Y, then pitch about the turned +X; no roll
    float r00 = 1 - 2 * (q.y * q.y + q.z * q.z), r01 = 2 * (q.x * q.y - q.z * q.w), r02 = 2 * (q.x * q.z + q.y * q.w);
    float r10 = 2 * (q.x * q.y + q.z * q.w), r11 = 1 - 2 * (q.x * q.x + q.z * q.z), r12 = 2 * (q.y * q.z - q.x * q.w);
    float r20 = 2 * (q.x * q.z - q.y * q.w), r21 = 2 * (q.y * q.z + q.x * q.w), r22 = 1 - 2 * (q.x * q.x + q.y * q.y);
    float vx = ox, vy = oy, vz = -od;
    return {q, {a.x + r00 * vx + r01 * vy + r02 * vz, a.y + r10 * vx + r11 * vy + r12 * vz, a.z + r20 * vx + r21 * vy + r22 * vz}};
}

// ---------------------------------------------------------------- per-session overlay state
struct Overlay {
    XrSession session = XR_NULL_HANDLE;
    ID3D11Device* dev = nullptr;
    XrSpace view = XR_NULL_HANDLE, local = XR_NULL_HANDLE;  // no LOCAL space = VIEW-locked fallback
    uint32_t maxLayers = 16;
    Anchor anchor;
    XrTime lastTime = 0;
    bool wasVisible = false;
} S;

static void Teardown() {
    for (Pane* p : {&g_banner, &g_viewers}) {
        if (p->sc) n_xrDestroySwapchain(p->sc);
        p->sc = XR_NULL_HANDLE;
        p->imgs.clear();
        p->hasImage = false;
        std::lock_guard<std::mutex> l(g_mx);
        p->dirty = true;  // a new session re-uploads the current pixels
    }
    if (S.view) n_xrDestroySpace(S.view);
    if (S.local) n_xrDestroySpace(S.local);
    S = Overlay{};
}

static bool Setup(XrSession session, XrSystemId sys, ID3D11Device* dev) {
    S.session = session;
    S.dev = dev;
    XrSystemProperties sp{XR_TYPE_SYSTEM_PROPERTIES};
    if (XR_SUCCEEDED(n_xrGetSystemProperties(g_instance, sys, &sp))) S.maxLayers = sp.graphicsProperties.maxLayerCount;

    XrReferenceSpaceCreateInfo rs{XR_TYPE_REFERENCE_SPACE_CREATE_INFO, nullptr, XR_REFERENCE_SPACE_TYPE_VIEW, {{0, 0, 0, 1}, {0, 0, 0}}};
    if (XR_FAILED(n_xrCreateReferenceSpace(session, &rs, &S.view))) return false;
    rs.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_LOCAL;
    if (g_followDeg > 0 && XR_FAILED(n_xrCreateReferenceSpace(session, &rs, &S.local))) {
        S.local = XR_NULL_HANDLE;
        Log("no LOCAL space; banner stays locked to the view");
    }

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
    g_bgra = fmt == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB || fmt == DXGI_FORMAT_B8G8R8A8_UNORM;

    for (Pane* p : {&g_banner, &g_viewers}) {
        XrSwapchainCreateInfo ci{XR_TYPE_SWAPCHAIN_CREATE_INFO};
        ci.usageFlags = XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT | XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT | XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
        ci.format = fmt;
        ci.sampleCount = 1;
        ci.width = p->w;
        ci.height = p->h;
        ci.faceCount = 1;
        ci.arraySize = 1;
        ci.mipCount = 1;
        if (XR_FAILED(n_xrCreateSwapchain(session, &ci, &p->sc))) return false;
        n_xrEnumerateSwapchainImages(p->sc, 0, &n, nullptr);
        p->imgs.assign(n, {XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR});
        if (XR_FAILED(n_xrEnumerateSwapchainImages(p->sc, n, &n, (XrSwapchainImageBaseHeader*)p->imgs.data()))) return false;
    }
    Log("session ready: format %lld, %u images, maxLayers %u", (long long)fmt, n, S.maxLayers);
    return true;
}

static bool Upload(Pane& P) {  // render thread only
    uint32_t idx;
    XrSwapchainImageAcquireInfo ai{XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO};
    if (XR_FAILED(n_xrAcquireSwapchainImage(P.sc, &ai, &idx)) || idx >= P.imgs.size()) return false;
    XrSwapchainImageWaitInfo wi{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO, nullptr, 100000000};  // 100 ms
    if (XR_FAILED(n_xrWaitSwapchainImage(P.sc, &wi))) return false;
    ID3D11DeviceContext* ctx = nullptr;
    S.dev->GetImmediateContext(&ctx);
    {
        std::lock_guard<std::mutex> l(g_mx);
        const uint32_t* px = P.pixels.data();
        std::vector<uint32_t> swz;
        if (g_bgra) {  // swap R and B
            swz.resize(P.pixels.size());
            for (size_t i = 0; i < swz.size(); ++i) {
                uint32_t p = P.pixels[i];
                swz[i] = (p & 0xFF00FF00) | (p & 0xFF) << 16 | (p >> 16 & 0xFF);
            }
            px = swz.data();
        }
        ctx->UpdateSubresource(P.imgs[idx].texture, 0, nullptr, px, P.w * 4, 0);
        P.dirty = false;
    }
    ctx->Release();
    XrSwapchainImageReleaseInfo ri{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};
    if (XR_FAILED(n_xrReleaseSwapchainImage(P.sc, &ri))) return false;
    P.hasImage = true;
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
    if (g_disabled || s != S.session || !g_banner.sc || !info) return n_xrEndFrame(s, info);
    try {
        // banner centred at g_up; the viewer quad's right edge at g_vRight (its box is right-aligned in the texture)
        struct { Pane* p; float x, y, d, w; } panes[] = {
            {&g_banner, 0, g_up, g_dist, g_width},
            {&g_viewers, g_vRight - g_vWidth / 2, g_vUp, g_vDist, g_vWidth}};
        XrCompositionLayerQuad quads[2];
        std::vector<const XrCompositionLayerBaseHeader*> layers(info->layers, info->layers + info->layerCount);
        bool anyVisible = false;
        {
            std::lock_guard<std::mutex> l(g_mx);
            ULONGLONG now = GetTickCount64();
            anyVisible = now < g_banner.until || now < g_viewers.until;
        }
        bool world = false;  // place in LOCAL this frame? Falls back to VIEW when tracking is lost.
        if (S.local && anyVisible) {
            XrSpaceLocation loc{XR_TYPE_SPACE_LOCATION};
            const XrSpaceLocationFlags need = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT;
            if (XR_SUCCEEDED(n_xrLocateSpace(S.view, S.local, info->displayTime, &loc)) && (loc.locationFlags & need) == need) {
                float yaw, pitch;
                HeadAngles(loc.pose.orientation, yaw, pitch);
                float dt = S.lastTime ? (float)((info->displayTime - S.lastTime) * 1e-9) : 0.f;
                Step(S.anchor, yaw, pitch, loc.pose.position.x, loc.pose.position.y, loc.pose.position.z, dt, !S.wasVisible);
                S.lastTime = info->displayTime;
                world = true;
            }
        }
        S.wasVisible = anyVisible;  // the next banner after a quiet spell appears straight ahead
        for (int i = 0; i < 2; ++i) {
            Pane& P = *panes[i].p;
            bool dirty, visible;
            {
                std::lock_guard<std::mutex> l(g_mx);
                dirty = P.dirty;
                visible = GetTickCount64() < P.until;
            }
            if (dirty && !Upload(P)) { Disable("swapchain upload failed"); return n_xrEndFrame(s, info); }
            if (!visible || !P.hasImage || layers.size() + 1 > S.maxLayers) continue;
            XrCompositionLayerQuad& q = quads[i];
            q = {XR_TYPE_COMPOSITION_LAYER_QUAD};
            q.layerFlags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT | XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT;
            q.space = world ? S.local : S.view;
            q.eyeVisibility = XR_EYE_VISIBILITY_BOTH;
            q.subImage = {P.sc, {{0, 0}, {P.w, P.h}}, 0};
            q.pose = world ? Place(S.anchor, panes[i].x, panes[i].y, panes[i].d)
                           : XrPosef{{0, 0, 0, 1}, {panes[i].x, panes[i].y, -panes[i].d}};
            q.size = {panes[i].w, panes[i].w * P.h / P.w};
            layers.push_back((const XrCompositionLayerBaseHeader*)&q);
        }
        if (layers.size() == info->layerCount) return n_xrEndFrame(s, info);
        XrFrameEndInfo mine = *info;
        mine.layerCount = (uint32_t)layers.size();
        mine.layers = layers.data();
        XrResult r = n_xrEndFrame(s, &mine);
        if (XR_SUCCEEDED(r)) return r;
        Log("xrEndFrame with overlay returned %d", (int)r);
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

// Self-test hook: rasterize viewers.txt content into a VW*VH RGBA buffer.
extern "C" __declspec(dllexport) void livechatxr_viewers_test(const char* u8, uint32_t* out) {
    std::wstring t(MultiByteToWideChar(CP_UTF8, 0, u8, -1, nullptr, 0), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, u8, -1, t.data(), (int)t.size());
    while (!t.empty() && t.back() == L'\0') t.pop_back();
    std::vector<uint32_t> px(VW * VH);
    RasterizeViewers(t, px);
    memcpy(out, px.data(), px.size() * 4);
}

// Self-test hook: advance a follow anchor. st = {yaw, pitch, x, y, z, valid, moving} (radians, metres), updated in
// place; pose7 = {qx, qy, qz, qw, px, py, pz} of a point 1 m straight ahead of the new anchor.
extern "C" __declspec(dllexport) void livechatxr_follow_test(float* st, float yaw, float pitch, float x, float y, float z,
                                                             float dt, int snap, float followDeg, float* pose7) {
    float keep = g_followDeg;
    g_followDeg = followDeg;
    Anchor a{st[0], st[1], st[2], st[3], st[4], st[5] != 0, st[6] != 0};
    Step(a, yaw, pitch, x, y, z, dt, snap != 0);
    g_followDeg = keep;
    float out[7] = {a.yaw, a.pitch, a.x, a.y, a.z, a.valid ? 1.f : 0.f, a.moving ? 1.f : 0.f};
    memcpy(st, out, sizeof out);
    XrPosef p = Place(a, 0, 0, 1);
    float o[7] = {p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w, p.position.x, p.position.y, p.position.z};
    memcpy(pose7, o, sizeof o);
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
        auto cfg = [&](const wchar_t* sec, const wchar_t* key, float& out, float lo, float hi) {
            wchar_t v[32];
            if (GetPrivateProfileStringW(sec, key, L"", v, 32, ini.c_str())) out = Clamp((float)_wtof(v), lo, hi, out);
        };
        float seconds = g_showMs / 1000.f;
        cfg(L"banner", L"up", g_up, -5, 5);
        cfg(L"banner", L"distance", g_dist, 0.1f, 10);
        cfg(L"banner", L"width", g_width, 0.05f, 5);
        cfg(L"banner", L"seconds", seconds, 0.5f, 600);
        cfg(L"banner", L"follow", g_followDeg, 0, 180);
        cfg(L"viewers", L"up", g_vUp, -5, 5);
        cfg(L"viewers", L"right", g_vRight, -5, 5);
        cfg(L"viewers", L"distance", g_vDist, 0.1f, 10);
        cfg(L"viewers", L"width", g_vWidth, 0.05f, 5);
        g_showMs = (ULONGLONG)(seconds * 1000);
        Log("negotiated in %ls (up %.2f, distance %.2f, width %.2f, %llu ms, follow %.0f deg; viewers up %.2f right %.2f width %.2f)",
            base, g_up, g_dist, g_width, g_showMs, g_followDeg, g_vUp, g_vRight, g_vWidth);
        g_active = true;
        std::thread(Watcher, &g_banner, L"banner.txt", g_showMs, Rasterize).detach();
        std::thread(Watcher, &g_viewers, L"viewers.txt", 20000ULL, RasterizeViewers).detach();
    });
    return XR_SUCCESS;
}