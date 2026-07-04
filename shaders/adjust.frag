#version 330 core
// Single-pass adjust shader. Mirrors render.apply_tune() line by line —
// keep the two in sync. Uniform values come from render.TuneUniforms.

in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_tex;
uniform float u_exp_gain;   // 2^(exposure * K_EXPOSURE_EV)
uniform vec3  u_wb;         // per-channel white-balance gains
uniform float u_contrast;   // slope around 0.5 in linear-ish space
uniform float u_hl;         // highlights pull, pre-scaled by K_HIGHLIGHTS
uniform float u_sh;         // shadows pull, pre-scaled by K_SHADOWS
uniform float u_sat;        // saturation mix factor
uniform mat3  u_hue;        // rotation around the gray axis

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);
const float GAMMA = 2.2;

void main() {
    vec3 c = texture(u_tex, v_uv).rgb;
    vec3 lin = pow(max(c, 0.0), vec3(GAMMA));
    lin *= u_exp_gain;
    lin *= u_wb;
    lin = (lin - 0.5) * u_contrast + 0.5;
    lin = max(lin, 0.0);
    vec3 srgb = pow(lin, vec3(1.0 / GAMMA));
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
