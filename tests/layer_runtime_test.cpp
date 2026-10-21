// Exercise the actual session setup and frame hook without a headset or D3D device.
#include "../layer/livechat_xr_layer.cpp"

static std::vector<XrReferenceSpaceType> created;
static std::vector<XrCompositionLayerQuad> submitted;
static XrPosef head;
static int located = 0;
static const XrSession SESSION = (XrSession)1;
static const XrSpace VIEW = (XrSpace)2, LOCAL = (XrSpace)3;

static XrResult XRAPI_CALL CreateSpace(XrSession, const XrReferenceSpaceCreateInfo* ci, XrSpace* out) {
    created.push_back(ci->referenceSpaceType);
    *out = ci->referenceSpaceType == XR_REFERENCE_SPACE_TYPE_VIEW ? VIEW : LOCAL;
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL SystemProperties(XrInstance, XrSystemId, XrSystemProperties*) {
    return XR_ERROR_RUNTIME_FAILURE;  // Setup retains its default layer limit.
}
static XrResult XRAPI_CALL Formats(XrSession, uint32_t capacity, uint32_t* count, int64_t* out) {
    *count = 1;
    if (capacity) *out = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL CreateSwapchain(XrSession, const XrSwapchainCreateInfo*, XrSwapchain* out) {
    *out = (XrSwapchain)4;
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL Images(XrSwapchain, uint32_t, uint32_t* count, XrSwapchainImageBaseHeader*) {
    *count = 0;  // Pre-uploaded panes below never touch D3D.
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL Locate(XrSpace view, XrSpace local, XrTime, XrSpaceLocation* out) {
    if (view != VIEW || local != LOCAL) return XR_ERROR_HANDLE_INVALID;
    ++located;
    out->locationFlags = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT;
    out->pose = head;
    return XR_SUCCESS;
}
static XrResult XRAPI_CALL EndFrame(XrSession, const XrFrameEndInfo* info) {
    submitted.clear();
    for (uint32_t i = 0; i < info->layerCount; ++i)
        submitted.push_back(*(const XrCompositionLayerQuad*)info->layers[i]);
    return XR_SUCCESS;
}

#define CHECK(condition) do { if (!(condition)) { fprintf(stderr, "line %d: %s\n", __LINE__, #condition); return 1; } } while (0)
static bool Near(float a, float b) { return fabsf(a - b) < 0.00001f; }

int main() {
    n_xrCreateReferenceSpace = CreateSpace;
    n_xrGetSystemProperties = SystemProperties;
    n_xrEnumerateSwapchainFormats = Formats;
    n_xrCreateSwapchain = CreateSwapchain;
    n_xrEnumerateSwapchainImages = Images;
    n_xrLocateSpace = Locate;
    n_xrEndFrame = EndFrame;
    const XrCompositionLayerBaseHeader* empty = nullptr;
    XrFrameEndInfo frame{XR_TYPE_FRAME_END_INFO};
    frame.layers = &empty;
    for (float follow : {0.f, 20.f}) {
        S = Overlay{};
        created.clear();
        located = 0;
        g_followDeg = follow;
        CHECK(Setup(SESSION, 1, nullptr));
        CHECK(created.size() == (follow == 0 ? 1u : 2u));
        CHECK(created[0] == XR_REFERENCE_SPACE_TYPE_VIEW);
        if (follow > 0) CHECK(created[1] == XR_REFERENCE_SPACE_TYPE_LOCAL);
        for (Pane* p : {&g_banner, &g_viewers}) {
            p->dirty = false;
            p->hasImage = true;
            p->until = GetTickCount64() + 60000;
        }
        for (int i = 0; i < 2; ++i) {
            // Both frames have nonidentity, changing tracked head poses.
            head = Place(Anchor{(i + 1) * PI / 4, .2f, float(i + 1), .5f, -.3f, true}, 0, 0, 0);
            frame.displayTime = 1000000000LL + i * 11111111LL;
            CHECK(h_xrEndFrame(SESSION, &frame) == XR_SUCCESS);
            CHECK(submitted.size() == 2);
            for (int pane = 0; pane < 2; ++pane) {
                const auto& q = submitted[pane];
                CHECK(q.space == (follow == 0 ? VIEW : LOCAL));
                if (follow == 0) {
                    CHECK(Near(q.pose.orientation.x, 0) && Near(q.pose.orientation.y, 0));
                    CHECK(Near(q.pose.orientation.z, 0) && Near(q.pose.orientation.w, 1));
                    CHECK(Near(q.pose.position.x, pane == 0 ? 0.f : g_vRight - g_vWidth / 2));
                    CHECK(Near(q.pose.position.y, pane == 0 ? g_up : g_vUp));
                    CHECK(Near(q.pose.position.z, pane == 0 ? -g_dist : -g_vDist));
                } else {
                    CHECK(!Near(q.pose.orientation.y, 0));
                }
            }
        }
        CHECK(located == (follow == 0 ? 0 : 2));
    }
    // Untrusted-size inputs: config values are clamped/finite and IPC files are read with a hard cap.
    CHECK(Clamp(100, 0.1f, 10, 1) == 10 && Clamp(-3, 0.1f, 10, 1) == 0.1f && Clamp(2, 0.1f, 10, 1) == 2);
    CHECK(Clamp(INFINITY, 0, 5, 1) == 1 && Clamp(NAN, 0, 5, 1) == 1);
    wchar_t tmp[MAX_PATH], file[MAX_PATH];
    CHECK(GetTempPathW(MAX_PATH, tmp) && GetTempFileNameW(tmp, L"lcx", 0, file));
    FILE* big = _wfopen(file, L"wb");
    CHECK(big);
    std::string chunk(1024 * 1024, 'x');
    fwrite(chunk.data(), 1, chunk.size(), big);
    fclose(big);
    std::string read;
    CHECK(ReadCapped(file, read) && read.size() == MAX_FILE);
    DeleteFileW(file);
    CHECK(!ReadCapped(file, read));
    puts("VIEW-locked zero offsets and LOCAL positive-follow control: OK");
    return 0;
}
