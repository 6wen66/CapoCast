Anime4K shaders by bloc97 (https://github.com/bloc97/Anime4K), MIT licensed: see Anime4K-LICENSE.txt.
Taken unmodified from commit 7684e9586f8dcc738af08a1cdceb024cc184f426 and joined into one file per quality,
because FFmpeg's libplacebo filter loads a single shader file.

anime4k_fast.glsl  Mode A:      Clamp_Highlights, Restore_CNN_M,  Upscale_CNN_x2_M,  AutoDownscalePre_x2, AutoDownscalePre_x4, Upscale_CNN_x2_S
anime4k_hq.glsl    Mode A (HQ): Clamp_Highlights, Restore_CNN_VL, Upscale_CNN_x2_VL, AutoDownscalePre_x2, AutoDownscalePre_x4, Upscale_CNN_x2_M
