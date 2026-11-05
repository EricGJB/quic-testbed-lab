# QUIC / 6G-Testbed 网络波动实验

**专题问答报告**解释 pacing 与 cwnd、稳时延和稳速率的价值、网络侧如何配合，并补做判丢阈值和分档交付分析。[阅读问答报告](REPORT.qa.zh-CN.md)

**第二版**使用本地 MiniCPM-o 2.6 视频问答，测试稳时延、稳速率、短时服务量和 pacing 协同对推理 QoE 的影响。[第二版报告](REPORT.v2.zh-CN.md) · [复现](v2/README.md) · [逐题结果](results/v2/requests.csv) · [配置汇总](results/v2/profiles.csv)

**第一版**的持续流和 GRACE 实验保留在下方。

在本机 WSL2 上，用真实 aioquic QUIC 和 Linux TCP/TLS 流量穿过三个网络命名空间和两对 veth。业务验证使用本地 GRACE-4096 编码的真实视频特征，经 QUIC DATAGRAM 传输后用本地模型重建。

- [实验报告](REPORT.zh-CN.md)
- [传输汇总](results/transport_summary.csv) · [逐轮指标](results/runs.csv) · [100 ms 有效吞吐](results/goodput_100ms.csv)
- [GRACE 画质和截止交付](results/grace_summary.csv) · [逐帧结果](results/grace_frames.csv)
- [网络配置](profiles.yaml) · [判丢阈值复验](followup.yaml)

## 架构

```text
client ns / c0  ->  router ns / r0, r1  ->  server ns / s0
  sender qlog         r1 egress: data        receiver qlog
  c0 PCAP             r0 egress: ACK         s0 PCAP
                      HTB and netem
```

run.py 是 Testbed 外部场景驱动。它验证上游四个组件文件与 Git blob 一致，直接调用原版 NetworkEmulator 加载 profile，并使用原版 TrafficLogger 写 SQLite。双端抓包、连接内阶跃和端点启动由场景驱动管理。网络规则只应用在独立 namespace，结束后删除这些 namespace。所有业务均为本地，传输无第三方模型 API。正式网络实验串行运行。

## 复现

Linux/WSL2，Python 3.12，iproute2、ethtool、tcpdump、openssl、git、sudo。运行输出写入 Linux 文件系统，留出约 1 GiB。试跑与正式数据使用不同目录。

```bash
git clone https://github.com/5G-MAG/6G-Testbed.git
git -C 6G-Testbed checkout 7231ae8e5670b67564cf28d397c2820d9403ecdc
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PY="$PWD/.venv/bin/python"
TESTBED="$PWD/6G-Testbed"
RAW="$HOME/quic-testbed-runs"
sudo "$PY" run.py --testbed "$TESTBED" --out "$RAW" --workload bulk --repeats 3
sudo "$PY" run.py --testbed "$TESTBED" --out "$RAW/followup" --workload bulk --repeats 3 --suite followup.yaml
sudo "$PY" run.py --testbed "$TESTBED" --out "$RAW/no-qlog" --workload bulk --repeats 3 --only fixed40 rate10ms --algorithm cubic --no-qlog
```

模型需要另行获取 [GRACE 上游](https://github.com/UChi-JCL/Grace/tree/14f1721663acd5d16ca776ce61b4a5860167d24d)、官方权重、光流初始化文件和 StreamingBench 前三个公开视频。沿用 [GRACE Testbed 适配项目](https://github.com/qinyishcn/grace-rtp-psnr-testbed/tree/62c3ae031d4d410ea98b0ff06f3df140d82135ee) 的环境和模型获取说明。模型命令需使用具备 CUDA PyTorch、torchvision、torchac、aiortc、Pillow、numpy 和相关上游依赖的 Python。通过适配项目的 grace_rtp.experiment prepare 取源帧时，指定以下参数。实测用既有缓存中的相同源帧，media.py 重新编码全部 42 张 P 帧。

```text
--videos 3 --frames 16 --gop 8 --fps 30 --start-seconds 10 --width 256 --height 192 --q 1 --scale 1
```

```bash
sudo "$MODEL_PY" media.py prepare --codec-repo "$CODEC_REPO" --grace "$GRACE" --cache "$CACHE" --output "$RAW"
sudo "$PY" run.py --testbed "$TESTBED" --out "$RAW" --workload grace --repeats 3
sudo "$MODEL_PY" media.py score --codec-repo "$CODEC_REPO" --grace "$GRACE" --cache "$CACHE" --output "$RAW"
sudo cp "$RAW"/{media.json,media.bin,iframes.bin,clean.npz} "$RAW/followup/"
sudo "$PY" run.py --testbed "$TESTBED" --out "$RAW/followup" --workload grace --repeats 3 --suite followup.yaml
sudo "$MODEL_PY" media.py score --codec-repo "$CODEC_REPO" --grace "$GRACE" --cache "$CACHE" --output "$RAW/followup"
"$PY" analyze.py "$RAW" results
"$PY" analyze.py "$RAW/followup" results/followup
"$PY" analyze.py "$RAW/no-qlog" results/no-qlog
"$PY" plots.py results "$RAW" --followup results/followup
"$PY" validate.py "$RAW" results
```

## 测量口径

- 持续流以收端应用已交付字节为准。每轮在 TLS 建立后计时 5.8 秒，主表报告全窗口，另存排除前 0.8 秒的值和每 100 ms 曲线。TCP 使用 TLS 1.3 与 CUBIC，内核和用户态栈差异仍然存在。
- QUIC 大流使用单 Stream、512 KiB 待确认数据上限和 16 MiB 初始连接及流信用。qlog 用来检查发送端流控阻塞帧。窗口低余量指有待发数据且可用拥塞信用小于一个最大数据报的**采样比例**。pacer 等待次数可包含对同一目标时间的多次检查。定时器迟到以进入回调的实际时间减去计划时间计算。
- 前后两处抓包使用同机时钟。按 IP ID、传输头和捕获到的加密前缀配对，仅对可匹配且已到达的包计算时延。反向包间隔按反向连接数据报统计，含少量控制帧。1 ms 突发用含包时间桶的 P95，正向只统计至少 1000 IP 字节的包。队列 backlog 是采样快照，含 netem 定时等待的字节。
- rate10ms 等名称中的数值是高低档的计划驻留时间，完整周期为其两倍。保留每次 tc change 的计划、调用、完成时刻。切换在原 qdisc 上执行，已排队的数据可能仍按此前的时刻释放。随机扰动用内核 RNG，各轮规则相同而实际丢包位置有差异。
- GRACE 使用 3 个视频、6 个 GOP、42 张 P 帧。I 帧 JPEG 和块清单先经可靠 Stream 校验交付，统计从定时播放 P 帧开始。缺片或超时则整个块置零，按受损的前一重建帧继续解码。收端对完整压缩块做 SHA-256 核对。50/100/200 ms 是同一次传输的三个离线判定截止时间，线上不主动撤销过期数据报。码流使用独立 DEFLATE 特征块。推理和传输分阶段执行，时延边界是 P 帧特征传输。
- 实验固定 aioquic 1.2.0。网络场景是 Linux 软件仿真，P95 与抓包时间戳刻画本机可观测行为。

## 公开数据

报告附逐轮汇总、时间序列和逐帧截止与 PSNR。Release 证据包含全部逐轮遥测和代表性场景的收发 qlog、头部 PCAP。完整逐包归档以及模型、视频、源特征、私钥和临时 TLS 证书留在本地。抓包仅含隔离链路的测试端口。[公开证据包](https://github.com/qinyishcn/quic-testbed-lab/releases/tag/v1.0.0)

由原始运行目录生成公开证据包的命令为 python archive.py RAW quic-testbed-evidence.tar.xz --compact。省略 compact 选项并使用 tar.gz 文件名可生成完整逐包归档。
