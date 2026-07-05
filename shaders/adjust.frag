#version 330 core
// Single-pass adjust shader. Mirrors render.apply_tune() line by line —
// keep the two in sync. Uniform values come from render.TuneUniforms.
//
// u_curve is the composed per-channel display-space tone curve
// (warmth ∘ tint ∘ brightness ∘ contrast) built by render.build_tone_curve
// — CURVE_N×1 RGB32F, sampled with explicit texelFetch+mix so the numpy
// export path (render.sample_curve) computes identical values.

in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_tex;
uniform sampler2D u_curve;  // CURVE_N×1 RGB32F per-channel tone curve
uniform sampler2D u_lmap;   // small blurred-luma map — ambiance input
uniform vec2 u_lmap_size;   // its (width, height); vec2 because Qt's
                            // setUniformValue has no integer-vector path
uniform float u_amb;        // ambiance in [-1, 1]
uniform float u_hl;         // highlights pull, pre-scaled by K_HIGHLIGHTS
uniform float u_sh;         // shadows pull, pre-scaled by K_SHADOWS
uniform float u_sat;        // saturation mix factor
uniform mat3  u_hue;        // rotation around the gray axis

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);
const int CURVE_N = 1024;

// render._AMB_COEFS flattened: rows i=1..4 (coef of p^i·(1-p)), cols L^0..L^5
const float AMB[24] = float[24](
    1.002303, 2.02708, -36.98477, 140.9541, -222.5687, 128.2808,
    -3.183862, 66.59705, -476.1331, 1211.945, -1232.374, 369.4713,
    -19.50465, 134.2402, 374.7766, -2583.244, 3844.742, -1674.701,
    52.76057, -563.3507, 1453.711, -898.8191, -807.9057, 739.3197);

vec3 tone_curve(vec3 v) {
    vec3 f = clamp(v, 0.0, 1.0) * float(CURVE_N - 1);
    ivec3 i0 = min(ivec3(f), ivec3(CURVE_N - 2));
    vec3 fr = f - vec3(i0);
    vec3 a = vec3(texelFetch(u_curve, ivec2(i0.r, 0), 0).r,
                  texelFetch(u_curve, ivec2(i0.g, 0), 0).g,
                  texelFetch(u_curve, ivec2(i0.b, 0), 0).b);
    vec3 b = vec3(texelFetch(u_curve, ivec2(i0.r + 1, 0), 0).r,
                  texelFetch(u_curve, ivec2(i0.g + 1, 0), 0).g,
                  texelFetch(u_curve, ivec2(i0.b + 1, 0), 0).b);
    return mix(a, b, fr);
}

void main() {
    vec3 srgb = tone_curve(texture(u_tex, v_uv).rgb);
    if (u_amb != 0.0) {
        // Ambiance: local tone map. Luma delta from (pixel luma, blurred
        // neighborhood luma), same for all channels — chroma-preserving;
        // the chroma component lives in u_sat. Mirrors render._amb_delta
        // + render._sample_lmap.
        vec2 fxy = clamp(v_uv, 0.0, 1.0) * u_lmap_size - 0.5;
        ivec2 i0 = ivec2(floor(fxy));
        vec2 fr = fxy - vec2(i0);
        ivec2 mx = ivec2(u_lmap_size) - ivec2(1);
        ivec2 lo = clamp(i0, ivec2(0), mx);
        ivec2 hi = clamp(i0 + ivec2(1), ivec2(0), mx);
        float L = mix(
            mix(texelFetch(u_lmap, ivec2(lo.x, lo.y), 0).r,
                texelFetch(u_lmap, ivec2(hi.x, lo.y), 0).r, fr.x),
            mix(texelFetch(u_lmap, ivec2(lo.x, hi.y), 0).r,
                texelFetch(u_lmap, ivec2(hi.x, hi.y), 0).r, fr.x), fr.y);
        float pl = dot(srgb, LUMA);
        float acc = 0.0;
        float pw = pl;
        for (int i = 0; i < 4; ++i) {
            float h = AMB[i * 6 + 5];
            for (int j = 4; j >= 0; --j) h = h * L + AMB[i * 6 + j];
            acc += pw * h;
            pw *= pl;
        }
        srgb = clamp(srgb + u_amb * (1.0 - pl) * acc, 0.0, 1.0);
        // vibrance: chroma-weighted saturation, strong on muted colors
        // (constants = render.K_AMB_VIB_POS / _NEG)
        float c2 = max(srgb.r, max(srgb.g, srgb.b))
                 - min(srgb.r, min(srgb.g, srgb.b));
        float k = u_amb > 0.0 ? 0.7108 - 0.7593 * c2 : 0.2297 - 0.2288 * c2;
        float gain = 1.0 + u_amb * max(k, 0.0);
        float pl2 = dot(srgb, LUMA);
        srgb = clamp(vec3(pl2) + (srgb - vec3(pl2)) * gain, 0.0, 1.0);
    }
    if (u_hl != 0.0 || u_sh != 0.0) {
        // Luma-masked pull toward white/black, applied as one gain to all
        // channels so color ratios survive a shadow lift (colorful, not washed)
        float L0 = dot(srgb, LUMA);
        float wh = L0 * L0;
        float ws = (1.0 - L0) * (1.0 - L0);
        float dL = (max(u_hl, 0.0) * (1.0 - L0) + min(u_hl, 0.0) * L0) * wh
                 + (max(u_sh, 0.0) * (1.0 - L0) + min(u_sh, 0.0) * L0) * ws;
        float gain = clamp(L0 + dL, 0.0, 1.0) / max(L0, 1e-4);
        srgb = clamp(srgb * gain, 0.0, 1.0);
    }
    float luma = dot(srgb, LUMA);
    vec3 sat = mix(vec3(luma), srgb, u_sat);
    fragColor = vec4(clamp(u_hue * sat, 0.0, 1.0), 1.0);
}
