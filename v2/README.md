# V2 复现

准备 Python 3.12、aioquic 1.2.0 和根目录 requirements.txt 中的依赖，使用 Linux/WSL2 下的 iproute2、tcpdump、ethtool、openssl、git、curl 和 sudo。测试按 [根目录 README](../README.md) 固定上游 Testbed 提交。

本地素材目录采用现有 MiniCPM 测试的布局。models 下包含 Model-7.6B-Q4_K_M.gguf 和 mmproj-model-f16.gguf，results/q4_k_m_four_native 下包含 questions_manifest.json，data/native_frames 下包含每题独立目录。题目 ID 中的空格在目录名里替换为下划线。每题目录的 frames.json 有四张原生时间顺序 JPEG 的文件名。自行获得模型、公开 StreamingBench 题目与视频并准备四张过去帧；[相关模型和帧准备流程](https://github.com/qinyishcn/ttft-inference-lab) 可供核对。视觉和推理源资产由运行方提供。

已有公开视频和 Real_Time_Visual_Understanding.csv 时，可安装 av、Pillow，执行 python v2/prepare_frames.py --questions CSV --videos VIDEO_DIR --assets ASSETS。视频文件按 001.mp4、002.mp4 等命名。不同解码器或 JPEG 库可能产生不同字节，可用逐题请求哈希核对同批输入。

两个 warmup 题取视频 2 的题 1–2。正式题取视频 1、4、7、10、13、16 的题 1–2。每个配置内的同题输入 SHA-256 必须一致。传输中含图像与问题，不发送标签。报告仅发布样本标识、字节数、哈希、选择题结果和遥测。

在仓库根目录，设置 PY、TESTBED、ASSETS、LLAMA、LLAMA_LIB_PATH、RAW、REPEAT 为实际绝对路径。LLAMA 是 llama.cpp CUDA build b11516 的 llama-server，LLAMA_LIB_PATH 含运行时、CUDA runtime 和 cuBLAS 动态库。

```bash
sudo "$PY" v2/run_qa.py --testbed "$TESTBED" --assets "$ASSETS" --llama "$LLAMA" --lib-path "$LLAMA_LIB_PATH" --out "$RAW"
sudo "$PY" v2/run_qa.py --testbed "$TESTBED" --assets "$ASSETS" --llama "$LLAMA" --lib-path "$LLAMA_LIB_PATH" --out "$REPEAT" \
  --grant-reference "$RAW" --only steady delay10 delay10_fifo delay_slot30 rate50_high rate50_low rate50_matched rate50_opposite grant_replay grant_inverse
"$PY" v2/analyze_qa.py "$RAW" results/v2
"$PY" v2/analyze_qa.py "$REPEAT" results/v2/repeat
"$PY" v2/plot_qa.py results/v2
"$PY" v2/publish_evidence.py "$RAW" "$REPEAT" quic-multimodal-v2-evidence.tar.xz
```

逐轮压缩遥测 JSONL 的 samples 元组按顺序记录相对毫秒、cwnd 字节、在途字节、平滑 RTT 秒、pacer 包时间秒、有待发数据、拥塞信用不足一个最大数据报、PTO 计数、当前 pacing 唤醒目标相对毫秒。waits 和 wakes 分别保存原生 pacer 等待检查与定时器唤醒时间，arrivals 保存服务器按序收到的累计字节，switches 保存计划、调用和完成时刻以及档位。
