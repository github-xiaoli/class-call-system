# 中文离线 TTS 引擎短名单（班级叫号器选型，2024–2025 开源）

调研方式：GitHub API 读取仓库元数据 + 各仓库 README/许可证原文；公开评测表（CosyVoice / IndexTTS / VoxCPM 的 Seed-TTS-Eval、CV3-Eval）。未执行任何安装或下载模型。自然度排序综合各公开评测（各表基准不同，仅作相对参考）。

> ⚠️ Python 3.13 兼容性：绝大多数候选锁定 Python 3.9–3.12（PyTorch/CUDA 生态，如 GPT-SoVITS 3.10–3.12、VoxCPM 要求 <3.13、CosyVoice 官方 3.10、F5-TTS ≥3.10）。建议“主程序 3.13 + 独立 TTS 子进程/本地服务（venv 3.10/3.11）”的架构，zip 内携带 venv 或安装脚本。

## 第一梯队（适合课堂叫号，自然度优先）

### 1. IndexTTS-2.5 — 哔哩哔哩（中文自然度顶级，Windows 友好）
- 仓库：https://github.com/index-tts/index-tts （stars≈25k）
- 许可证：bilibili Model Use License（月活 <1 亿且年收入 <10 亿元人民币可免费用；超出需单独授权）
- 模型：IndexTTS-2.5 = 0.8B；IndexTTS-2 = 1.5B（另有 1.0/1.5 旧版）
- 实时性/显存：RTF≈0.21（RTX 4090, bf16）；支持 fp16/bf16（官方未标 VRAM，估 4–6 GB）；纯 CPU 未承诺、慢
- 嵌入方式：Python API（`from indextts.infer_v2_5 import IndexTTS2`）、WebUI、vLLM
- Windows：官方支持（uv 安装，DeepSpeed 可跳过）
- 发音示例/评测：https://index-tts.github.io/index-tts2-5.github.io ；B站 BV1uvMk6ZEdK；CV3-Eval zh WER 4.36 / SS 77.1

### 2. CosyVoice3 / CosyVoice2-0.5B — 阿里 FunAudioLLM（最稳、生态最全）
- 仓库：https://github.com/FunAudioLLM/CosyVoice （现重定向至 QwenAudio/CosyVoice，stars≈24k）
- 许可证：Apache-2.0（仓库）；权重见 HF/ModelScope 页面
- 模型：CosyVoice2-0.5B、Fun-CosyVoice3-0.5B（另有 300M 轻量版）
- 实时性/显存：GPU 流式实时（官方称首包延迟低至 150ms）；0.5B fp16 估 2–4 GB；纯 CPU 可跑但难实时（无官方 CPU 基准）
- 嵌入方式：Python API（example.py）、FastAPI/gRPC 服务、WebUI、vLLM/TensorRT-LLM
- Windows：官方文档偏 Linux（ttsfrd 仅 Linux，默认回退 wetext 可跨平台）；社区可在 Windows 运行，需小改
- 发音示例/评测：https://funaudiollm.github.io/cosyvoice2 、https://funaudiollm.github.io/cosyvoice3 ；zh CER 1.45 / SS 75.7（CV2），CER 1.21 / SS 78.0（CV3）

### 3. VoxCPM 1.5 / 0.5B — OpenBMB 面壁·清华（Apache-2.0 新秀，可商用）
- 仓库：https://github.com/OpenBMB/VoxCPM （stars≈42k）
- 许可证：Apache-2.0（代码+权重，可商用）
- 模型：VoxCPM-0.5B（0.6B 主干）、VoxCPM1.5（0.8B）、VoxCPM2（2B）
- 实时性/显存：RTF≈0.15–0.30（4090）；官方 VRAM ≈5/6/8 GB；CPU 有 GGUF/ONNX 路线但慢（M4 Pro Metal RTF≈1.76）
- 嵌入方式：`pip install voxcpm` Python API（generate / generate_streaming）、CLI、WebUI、vLLM-Omni
- Windows：pip 跨平台，`--device auto/cpu/cuda`（未特化 Windows 说明，应可行）
- 发音示例/评测：https://openbmb.github.io/voxcpm2-demopage ；Seed-TTS-Eval zh CER 0.93 / SS 77.2（0.5B）、0.97 / 79.5（VoxCPM2）

### 4. GPT-SoVITS — RVC-Boss（想用某位老师本人声音时首选，CPU 友好）
- 仓库：https://github.com/RVC-Boss/GPT-SoVITS （stars≈65k）
- 许可证：MIT
- 模型：未官方标注参数量（GPT+SoVITS+BigVGAN，预训练包约百 MB~GB 级）
- 实时性/显存：RTF 0.028@4060Ti、0.014@4090；M4 CPU RTF 0.526（约 2× 实时），x86 台式 CPU 短句可近实时；显存需求低
- 嵌入方式：Python 模块 + WebUI + API；官方 Windows 一体包（win≥10，双击 bat）
- Windows：官方 Windows 一体包（最省事）
- 发音示例/评测：B站 BV12g4y1m7Uw；零样本/少样本克隆（5 秒参考音频即可，1 分钟微调更佳）

## 第二梯队（可用但有取舍）

### 5. fish-speech S2-Pro — Fish Audio（中文 WER 极低，但重且许可证受限）
- 仓库：https://github.com/fishaudio/fish-speech （stars≈34k）
- 许可证：FISH AUDIO COMMUNITY LICENSE（研究/非商用免费；商用限年收入阈值；Hosted 服务需单独授权）
- 模型：S2-Pro 4B；Drama 3 35B-A3B（旗舰）
- 实时性/显存：RTF 0.195（H200）；4B 需 ~8 GB+ 显存；纯 CPU 不可实时
- 嵌入方式：CLI / API server / WebUI；Linux/GPU 优先
- 发音示例/评测：https://speech.fish.audio ；Seed-TTS-Eval zh WER 0.54（官方自报）

### 6. F5-TTS — 上海交大 X-LANCE（轻量、MIT 代码）
- 仓库：https://github.com/SWivid/F5-TTS （stars≈15.6k）
- 许可证：代码 MIT；预训练权重 CC-BY-NC（训练数据 Emilia 所致）
- 模型：0.3B
- 实时性/显存：L20 RTF 0.1467（PyTorch）；GPU 实时；纯 CPU 可跑但慢（16 步扩散）
- 嵌入方式：`pip install f5-tts` CLI / Gradio / Python
- Windows：PyTorch 跨平台，社区可跑
- 发音示例/评测：https://swivid.github.io/F5-TTS ；zh CER 1.52 / SS 74.1

### 7. ChatTTS — 2noise（对话感/韵律强，但权重非商用且偶不稳定）
- 仓库：https://github.com/2noise/ChatTTS （stars≈40k）
- 许可证：代码 AGPL-3.0+；模型权重 CC BY-NC 4.0（限非商用）
- 模型：官方未标注参数量（40k 小时预训练）
- 实时性/显存：官方 FAQ：≥4 GB 显存；4090 RTF≈0.3；纯 CPU 可跑但慢
- 嵌入方式：`pip install ChatTTS` Python API、WebUI、CLI
- Windows：PyTorch 跨平台可跑
- 发音示例/评测：B站 BV1zn4y1o7iV（官方介绍视频）

### 8. MegaTTS3 — 字节跳动（Apache-2.0，但克隆受限、CPU 慢）
- 仓库：https://github.com/bytedance/MegaTTS3 （stars≈6.1k）
- 许可证：Apache-2.0
- 模型：0.45B DiT 主干（中英）
- 实时性/显存：CPU 可用但官方称一条约 30 秒（10 步）；GPU 较快（未官方标 RTF/VRAM）
- 嵌入方式：CLI + Gradio（Python）
- Windows：官方“Windows 版本测试中”（注释 WeTextProcessing）
- 限制：WaveVAE 编码器权重未开源，音色克隆需官方预提取 .npy latent
- 发音示例/评测：HuggingFace Space ByteDance/MegaTTS3

### 9. Kokoro-82M — hexgrad（极轻、CPU 实时，中文自然度中等）
- 仓库：https://github.com/hexgrad/kokoro （stars≈10k）
- 许可证：Apache-2.0（权重同）
- 模型：82M
- 实时性/显存：纯 CPU 轻松实时；几乎不占显存
- 嵌入方式：`pip install kokoro`（KPipeline，lang_code='z' 普通话，需 `misaki[zh]` + espeak-ng）
- Windows：明确支持（espeak-ng 装 .msi）
- 发音示例/评测：https://huggingface.co/hexgrad/Kokoro-82M/blob/main/SAMPLES.md （中文音色少）

### 10. MeloTTS — MyShell/MIT（MIT、CPU 实时，自然度中等）
- 仓库：https://github.com/myshell-ai/MeloTTS （stars≈7.7k）
- 许可证：MIT
- 模型：轻量 VITS（官方未标参数量；CPU 实时）
- 实时性/显存：官方明确 CPU 实时；中英混读
- 嵌入方式：`pip install melotts` Python API
- Windows：跨平台可跑
- 发音示例/评测：README 内置中文示例（S3 链接）

### 11. Bert-VITS2 — fishaudio（已停维护，不推荐新项目）
- 仓库：https://github.com/fishaudio/Bert-VITS2 （stars≈8.8k）
- 许可证：AGPL-3.0；官方宣布“短期不再维护”，推荐 fish-speech/GPT-SoVITS 替代
- 模型：VITS2+多语言 BERT（需训练音色，非零样本优先）
- 实时性/显存：VITS 类 CPU 可实时
- Windows：可跑（WebUI）
- 发音示例/评测：B站 BV1hp4y1K78E

## 关于 OpenAudio
GitHub 上未检索到以“OpenAudio”命名的高星中文 TTS 仓库（检索 top 结果为 webprofusion/OpenAudio，.NET 音频库，与 TTS 无关）。在 VoxCPM 的 Seed-TTS-Eval 表中出现过 “OpenAudio-s1-mini（0.5B，开源，zh CER 1.18 / SIM 68.5）” 一条，可能是某评测条目而非独立知名仓库；若确指该项目，建议再按模型卡核对仓库与许可证。

## 结论（一句话）
追求“自然 + 可离线 + 可嵌入 + 稳定”，**首选阿里 CosyVoice2-0.5B**（Apache-2.0、0.5B、Python API/流式部署最全、中文自然且稳定），**次选哔哩哔哩 IndexTTS-2.5**（中文最自然、Windows 友好，但许可证与 GPU 门槛略高）；若目标机器无独立显卡、必须纯 CPU 实时，则退而选 **GPT-SoVITS（MIT，Windows 一体包，可克隆老师本人音色）**，并以 **Kokoro-82M / MeloTTS** 作轻量兜底。
