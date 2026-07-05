#version 330 core
// Single-pass adjust shader. Mirrors render.apply_tune() line by line —
// keep the two in sync. Uniform values come from render.TuneUniforms.
//
// u_curve is the composed per-channel display-space tone curve
// (warmth ∘ tint ∘ brightness ∘ contrast) built by render.build_tone_curve
// — CURVE_N×1 RGB32F, sampled with explicit texelFetch+mix so the numpy
// export path (render.sample_curve) computes identical values.

in vec2 v_uv;
in vec2 v_frame;            // position in the visible frame [0,1]²
out vec4 fragColor;

uniform sampler2D u_tex;
uniform sampler2D u_curve;  // CURVE_N×1 RGB32F per-channel tone curve
uniform sampler2D u_lmap;   // small blurred-luma map — ambiance input
uniform vec2 u_lmap_size;   // its (width, height); vec2 because Qt's
                            // setUniformValue has no integer-vector path
uniform float u_amb;        // ambiance in [-1, 1]
uniform float u_hl;         // highlights in [-1, 1]
uniform float u_sh;         // shadows in [-1, 1]
uniform float u_sat;        // saturation mix factor
uniform float u_vig;        // vignette strength (0 = off; sign is baked
                            // into u_vig_curve, this only gates the stage)
uniform sampler2D u_vig_curve;  // brightness curve at the vignette strength
uniform vec2 u_vig_center;  // in visible-frame coords
uniform float u_vig_radius; // fraction of the half frame diagonal
uniform vec2 u_vig_frame;   // visible frame size in pixels (aspect)

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);
const int CURVE_N = 1024;

// render.HLSH_CHROMA flattened: (b, h0..h6) per side, order hl+ hl- sh+ sh-
const float HLSH[32] = float[32](
    0.0352, 0.137029, 0.260296, -1.32709, 0.644387, 0.0192083, 0.0383106,
    -0.212314,
    1.5921, 0.350513, -2.65793, 6.37245, -4.58377, -0.8328, 1.11377, 1.20787,
    1.2671, 0.734066, -0.777532, 0.016477, 0.153334, -1.37066, 0.670391,
    0.753031,
    -0.0976, -0.393829, 1.24253, 0.215181, -1.09558, -0.894893, 0.480137,
    0.55739);

// render._AMB_COEFS flattened: rows i=1..4 (coef of p^i·(1-p)), cols L^0..L^5
const float AMB[24] = float[24](
    1.002303, 2.02708, -36.98477, 140.9541, -222.5687, 128.2808,
    -3.183862, 66.59705, -476.1331, 1211.945, -1232.374, 369.4713,
    -19.50465, 134.2402, 374.7766, -2583.244, 3844.742, -1674.701,
    52.76057, -563.3507, 1453.711, -898.8191, -807.9057, 739.3197);

float hlsh_D(int tool, float sgn, float x) {
    // gray response of one highlights/shadows side (render._hlsh_D)
    if (tool == 0) {
        if (sgn > 0.0) return 0.2571 * x;
        float t = max(0.0, (x - 0.55) / 0.45);
        return -(t * (0.168555 + t * (0.310803 - 0.308744 * t)));
    }
    if (sgn > 0.0) {
        float t = max(0.0, (0.30 - x) / 0.30);
        return t * (0.252738 + t * (0.028237 - 0.132882 * t));
    }
    return -0.2478 * (1.0 - x);
}

vec3 hlsh_apply(vec3 v, float s, int tool) {
    // measured Snapseed highlights/shadows: delta driven by the channel
    // value AND the pixel luma — mirrors render._hlsh_apply
    if (s == 0.0) return v;
    float sgn = sign(s);
    float L = dot(v, LUMA);
    float DL = hlsh_D(tool, sgn, L);
    vec3 Dp = vec3(hlsh_D(tool, sgn, v.r), hlsh_D(tool, sgn, v.g),
                   hlsh_D(tool, sgn, v.b));
    int o = (tool == 0 ? (sgn > 0.0 ? 0 : 8) : (sgn > 0.0 ? 16 : 24));
    vec3 h = vec3(HLSH[o + 1] + HLSH[o + 2] * L + HLSH[o + 3] * L * L
                  + HLSH[o + 4] * L * L * L)
           + HLSH[o + 5] * v + HLSH[o + 6] * v * v + HLSH[o + 7] * v * L;
    vec3 d = abs(s) * (vec3(DL) + HLSH[o] * (Dp - vec3(DL))
                       + (v - vec3(L)) * h);
    return clamp(v + d, 0.0, 1.0);
}

vec3 curve_sample(sampler2D curve, vec3 v) {
    vec3 f = clamp(v, 0.0, 1.0) * float(CURVE_N - 1);
    ivec3 i0 = min(ivec3(f), ivec3(CURVE_N - 2));
    vec3 fr = f - vec3(i0);
    vec3 a = vec3(texelFetch(curve, ivec2(i0.r, 0), 0).r,
                  texelFetch(curve, ivec2(i0.g, 0), 0).g,
                  texelFetch(curve, ivec2(i0.b, 0), 0).b);
    vec3 b = vec3(texelFetch(curve, ivec2(i0.r + 1, 0), 0).r,
                  texelFetch(curve, ivec2(i0.g + 1, 0), 0).g,
                  texelFetch(curve, ivec2(i0.b + 1, 0), 0).b);
    return mix(a, b, fr);
}

void main() {
    vec3 srgb = curve_sample(u_curve, texture(u_tex, v_uv).rgb);
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
    srgb = hlsh_apply(srgb, u_hl, 0);
    srgb = hlsh_apply(srgb, u_sh, 1);
    float luma = dot(srgb, LUMA);
    vec3 outc = clamp(mix(vec3(luma), srgb, u_sat), 0.0, 1.0);
    if (u_vig != 0.0) {
        // Vignette: blend toward the brightness curve at the vignette
        // strength, weighted by the fitted radial falloff. Mirrors the
        // vignette block in render.apply_tune / render.vignette_weight.
        vec2 dpx = (v_frame - u_vig_center) * u_vig_frame;
        float half_diag = 0.5 * length(u_vig_frame);
        float d = length(dpx) / (u_vig_radius * half_diag);
        float t = clamp((d - 0.10) / (1.24 - 0.10), 0.0, 1.0);
        float m = min(1.174 * t * t * (3.0 - 2.0 * t), 1.0);
        outc = mix(outc, curve_sample(u_vig_curve, outc), m);
    }
    fragColor = vec4(outc, 1.0);
}
