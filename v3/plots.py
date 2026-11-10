"""根据第三版实验数据生成中文图表。"""

import argparse
import csv
import gzip
import io
import json
import tarfile
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

COLORS = ["#657186", "#968153", "#147b8e", "#b65e43", "#78498e"]
RATE = "速率（Mbps）"
BYTES = "数据量（kB）"
TIME = "时间（ms）"
TASK_TIME = "从提交请求开始计时（ms）"
TASK_SIZES = ["40 kB", "200 kB", "1 MB"]
PIPE_NAMES = ("普通管道（短时波动）", "恒定速率管道", "稳定高容量管道", "普通管道（长时波动）")
GRANT_NAMES = ("固定分配 20 Mbps", "容量在 10 和 30 Mbps 之间切换",
               "按需分配，发送端自行调节", "按需分配，发送端提前适配")
PACING_NAMES = ("由拥塞控制算法自行调节", "固定使用 28 Mbps",
                "先用 28 Mbps，后用 14 Mbps",
                "固定使用 14 Mbps", "固定使用 21 Mbps")


def set_chinese_font(path):
    if path:
        font_manager.fontManager.addfont(str(path))
        family = font_manager.FontProperties(fname=path).get_name()
    else:
        families = {font.name for font in font_manager.fontManager.ttflist}
        family = next((name for name in ("Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC",
            "Source Han Sans SC", "SimHei", "PingFang SC", "WenQuanYi Zen Hei") if name in families), None)
        if not family:
            raise SystemExit("需要支持简体中文的字体。请安装思源黑体，或用 --font 指定字体文件。")
    matplotlib.rcParams.update({"font.family": family, "axes.unicode_minus": False, "font.size": 10})


def style(ax, title, ylabel):
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=.23)
    ax.spines[["right","top"]].set_visible(False)


def figure(out, name, fig):
    fig.savefig(out/(name+".png"),dpi=160,bbox_inches="tight",facecolor="white")
    plt.close(fig)


def plot_window_and_pacing(out):
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.2), layout="constrained")
    fig.suptitle("拥塞窗口与目标发包速率怎样配合（原理示例）", fontweight="bold", fontsize=16)
    window, pacing, limits, feedback = axes.flatten()
    active, free = COLORS[2], "#e6edf3"

    window.set_title("（甲）拥塞窗口限制同时在途的数据量", loc="left", fontweight="bold")
    for y, used in [(1, 4.8), (0, 2.4)]:
        window.barh(y, used, height=.5, color=active)
        window.barh(y, 6-used, left=used, height=.5, color=free)
        window.text(used/2, y, f"在途 {used:.1f} kB", color="white", ha="center", va="center")
        window.text((used+6)/2, y, f"余量\n{6-used:.1f} kB", color="#23354a",
                    ha="center", va="center", fontsize=9)
    window.set_yticks([1, 0], ["收到确认前", "两个包获确认后"])
    window.set_xlim(0, 6)
    window.set_ylim(-.65, 1.55)
    window.set_xticks([0, 1.2, 2.4, 3.6, 4.8, 6])
    window.set_xlabel("数据量（kB），示例中的拥塞窗口为 6 kB")
    window.spines[["right", "top", "left"]].set_visible(False)
    window.tick_params(axis="y", length=0)

    style(pacing, "（乙）目标发包速率决定理想发包间隔", "")
    for y, gap, count, color in [(1, .8, 5, COLORS[0]), (0, .4, 9, active)]:
        times = [i*gap for i in range(count)]
        pacing.scatter(times, [y]*count, s=52, color=color, zorder=3)
        pacing.annotate("", xy=(gap, y+.23), xytext=(0, y+.23),
                        arrowprops={"arrowstyle": "<->", "color": color, "lw": 1.2})
        pacing.text(gap/2, y+.29, f"{gap:.1f} ms", ha="center", va="bottom", color="#23354a")
    pacing.set_yticks([1, 0], ["目标为 12 Mbps", "目标为 24 Mbps"])
    pacing.set_xlim(-.12, 3.35)
    pacing.set_ylim(-.42, 1.62)
    pacing.set_xticks([i*.4 for i in range(9)])
    pacing.set_xlabel("发送时间（ms），每个圆点代表一个 1.2 kB 的包")

    style(limits, "（丙）到达发包时刻且窗口有余量，发送端才能发包", "")
    times = list(range(10))
    blocked = [2, 3, 6, 7]
    limits.scatter(times, [1]*len(times), s=52, color=active, label="实际发出一个包", zorder=3)
    sent = [t for t in times if t not in blocked]
    limits.scatter(sent, [0]*len(sent), s=52, color=active, zorder=3)
    limits.scatter(blocked, [0]*len(blocked), s=60, facecolors="none", edgecolors=COLORS[3],
                   linewidths=1.5, label="窗口已满，暂缓发送", zorder=3)
    limits.axvline(4, color="#535b64", linestyle="--", lw=1, label="第一个包的确认到达")
    limits.set_yticks([1, 0], ["拥塞窗口为 4.8 kB", "拥塞窗口为 2.4 kB"])
    limits.set_xticks(times)
    limits.set_xlim(-.28, 9.28)
    limits.set_ylim(-.48, 1.73)
    limits.set_xlabel("时间（ms），示例中的目标速率为 9.6 Mbps，往返时延为 4 ms")
    limits.legend(loc="upper center", fontsize=8, ncol=3)
    limits.grid(axis="x", alpha=.16)

    feedback.set_title("（丁）网络容量变化时，发包目标和窗口目标可以分开变化",
                       loc="left", fontweight="bold")
    feedback.set_axis_off()
    matrix = feedback.table(
        cellText=[
            ["网络容量（Mbps）", "30", "30", "10", "10"],
            ["瓶颈带宽估计（Mbps）", "30", "30", "30", "10"],
            ["目标发包速率（Mbps）", "30", "37.5", "30", "10"],
            ["拥塞窗口基础目标（kB）", "30", "30", "30", "10"],
        ],
        colLabels=["比较指标", "常规发送", "短时探测", "刚降速，\n估计尚未更新", "若算法采纳\n较低的估计"],
        colWidths=[.28, .15, .15, .21, .21], cellLoc="center", bbox=(0, .14, 1, .78),
    )
    matrix.auto_set_font_size(False)
    matrix.set_fontsize(9.2)
    for (row, column), cell in matrix.get_celld().items():
        cell.set_edgecolor("white")
        cell.set_facecolor("#e6edf3" if row == 0 or column == 0 else "#f3f6f9")
        if (row, column) in {(3, 2), (1, 3), (3, 3), (4, 3), (2, 4), (3, 4), (4, 4)}:
            cell.set_facecolor("#d9ecee" if column != 3 else "#f5e4de")
            cell.get_text().set_weight("bold")
    feedback.text(0, .05, "窗口基础目标按 4 ms 的最小往返时延和 2 倍窗口系数计算。",
                  transform=feedback.transAxes, fontsize=9)
    figure(out, "cwnd-pacing-principle", fig)


def plot_a(out, runs, long, summary):
    names = ["random", "equal20", "gold", "slow"]
    labels = ["普通管道\n短时波动", "恒定速率\n管道", "稳定高容量\n管道", "普通管道\n长时波动"]
    active = [[r for r in runs if r["case"] == "A1load_"+case] for case in names]
    fig, ax = plt.subplots(2,2,figsize=(12,8),layout="constrained")
    for a, field, heading, unit in ((ax[0,0],"pace","（甲）持续有待发数据时的目标发包速率",RATE),
                                   (ax[0,1],"mismatch","（乙）目标发包速率与模拟空口容量的绝对差",RATE)):
        for i,rows in enumerate(active):
            if not rows:
                continue
            lo,p50,hi = (median(r[field][j] for r in rows) for j in (0,1,2))
            a.errorbar(i,p50,yerr=[[p50-lo],[hi-p50]],fmt="o",color=COLORS[i],capsize=6)
        a.set_xticks(range(len(names)),labels)
        style(a,heading,unit)
    for case,c,label in zip(names,COLORS,PIPE_NAMES):
        xs = [r for r in long if r["case"] == "A1load_"+case and r["repeat"] == "0"]
        if xs:
            x = [float(r["time_ms"])/1000 for r in xs]
            y = [float(r["btlbw_mbps"]) for r in xs]
            ax[1,0].plot(x,y,color=c,alpha=.85,label=label,linewidth=1)
    style(ax[1,0],"（丙）拥塞控制算法的瓶颈带宽估计（示例轮次）",RATE)
    ax[1,0].set_xlabel("从实验计时起点开始计时（s）")
    ax[1,0].legend(fontsize=8,ncol=2)
    periodic = [summary["A1_"+case,"all"] for case in names[:3]]
    bars = ax[1,1].bar(range(3),[100*int(r["on_time"])/int(r["tasks"]) for r in periodic],color=COLORS[:3])
    ax[1,1].bar_label(bars,fmt="%.1f%%")
    ax[1,1].set_xticks(range(3),labels[:3]); ax[1,1].set_ylim(0,113)
    style(ax[1,1],"（丁）每 20 ms 提交 40 kB 请求的上传表现","20 ms 内上传完成的请求占比（%）")
    tail = ax[1,1].twinx()
    tail.plot(range(3),[float(r["token_p99"]) for r in periodic],"k:o",label="传输回执耗时的第 99 百分位数")
    tail.set_ylabel("传输回执耗时的第 99 百分位数（ms）")
    tail.set_ylim(0,32)
    tail.spines["top"].set_visible(False)
    tail.legend(loc="lower right",fontsize=8)
    figure(out,"A1",fig)


def plot_a_slow(out):
    evidence = out / "trace-evidence.tar.xz"
    if not evidence.is_file():
        return
    sample_dir = "A1load_slow__00/"
    with tarfile.open(evidence) as archive:
        try:
            plan = json.load(archive.extractfile(sample_dir + "plan.json"))
        except KeyError:
            return
        with io.TextIOWrapper(archive.extractfile(sample_dir + "samples.csv"), encoding="utf-8") as stream:
            pacing = [((int(row["us"]) - plan["origin"]) / 1e6, int(row["pacing_bps"]) / 1e6)
                      for row in csv.DictReader(stream)]
    capacity = [(offset / 1e6, rate) for offset, rate in plan["capacity"]]

    def step(ax, trace, left, right, **kwargs):
        times, values = zip(*trace)
        first = bisect_right(times, left)
        last = bisect_right(times, right)
        ax.step((left, *times[first:last], right), (values[first-1], *values[first:last], values[last-1]),
                where="post", **kwargs)

    fig, axes = plt.subplots(2, 1, figsize=(11, 6.6), layout="constrained", sharey=True)
    fig.suptitle("A1 长时波动补测中的目标发包速率与网络速率", fontweight="bold")
    intervals = [(3, 6, "（甲）第 3 s 至第 6 s 的完整统计区间"),
                 (3, 3.7, "（乙）放大第 3 s 至第 3.7 s 的两次降速")]
    axes[0].axvspan(3, 3.7, color="#e9eef5", alpha=.75, label="乙图放大区间")
    for ax, (left, right, title) in zip(axes, intervals):
        step(ax, pacing, left, right, color=COLORS[2], linewidth=1.5, label="目标发包速率（BBR）")
        step(ax, capacity, left, right, color="#333333", linestyle="--", linewidth=1.3,
             label="网络速率（模拟空口容量）")
        ax.set_xlim(left, right)
        ax.set_ylim(0, 42)
        ax.set_xlabel("从实验计时起点开始计时（s）")
        style(ax, title, RATE)
    axes[0].legend(loc="lower center", ncol=3, fontsize=9)
    figure(out, "A1-slow-rates", fig)


def aggregate(curves, case, size, field):
    by_ms = defaultdict(list)
    for r in curves[case,size]:
        by_ms[int(float(r["ms"]))].append(float(r[field]))
    return [(t,median(v)) for t,v in sorted(by_ms.items())]


def plot_b(out, curves, summary):
    long_rtt = ("B1_rtt_fast", "200000") in summary
    fig, ax = plt.subplots(3 if long_rtt else 2,2,figsize=(12,10 if long_rtt else 8),layout="constrained")
    names = ["fixed","variable","elastic","fast"]
    for idx,name in enumerate(names):
        a = ax[0,0]
        summary_rows = [summary["B1_"+name,str(size)] for size in (40000,200000,1000000)]
        a.bar([i+(idx-1.5)*.18 for i in range(3)],
              [100*int(r["relaxed_on_time"])/int(r["tasks"]) for r in summary_rows],.17,
              color=COLORS[idx],label=GRANT_NAMES[idx])
    ax[0,0].set_xticks(range(3),[f"{size}\n时限 {budget} ms" for size,budget in zip(TASK_SIZES,(24,70,310))])
    ax[0,0].legend(fontsize=8,ncol=2,loc="upper center")
    ax[0,0].set_ylim(0,137); ax[0,0].set_yticks(range(0,101,20))
    ax[0,0].set_xlabel("请求大小及对应的补测时限")
    style(ax[0,0],"（甲）补测时限内的上传完成率","按时完成的请求占比（%）")
    for a,field,unit,title,scale in ((ax[0,1],"pacing_mbps",RATE,"（乙）200 kB 请求的目标发包速率",1),
        (ax[1,0],"cwnd_bytes",BYTES,"（丙）200 kB 请求的拥塞窗口",.001),
        (ax[1,1],"queue",BYTES,"（丁）200 kB 请求的中继队列",.001)):
        for idx,name in enumerate(names[2:],2):
            pts=aggregate(curves,"B1_"+name,200000,field)
            a.plot([t for t,v in pts if t<=30],[v*scale for t,v in pts if t<=30],color=COLORS[idx],label=GRANT_NAMES[idx])
        if field == "pacing_mbps": a.axhline(30,color="black",linestyle=":",label="网络分配速率为 30 Mbps")
        a.legend(fontsize=8); a.set_xlabel(TASK_TIME)
        style(a,title,unit)
    if long_rtt:
        for idx, name in enumerate(("B1_rtt_elastic", "B1_rtt_fast"),2):
            pts = aggregate(curves,name,200000,"cwnd_bytes")
            ax[2,0].plot([t for t,v in pts],[v*.001 for t,v in pts],color=COLORS[idx],label=GRANT_NAMES[idx])
            row = [summary[name,str(size)] for size in (40000,200000,1000000)]
            p50=[float(r["upload_p50"]) for r in row]
            p99=[float(r["upload_p99"]) for r in row]
            ax[2,1].bar([i+(idx-2.5)*.28 for i in range(3)],p50,.25,color=COLORS[idx],label=GRANT_NAMES[idx],
                        yerr=[[0]*3,[b-a for a,b in zip(p50,p99)]],capsize=4)
        ax[2,0].legend(fontsize=8); ax[2,0].set_xlabel(TASK_TIME)
        ax[2,1].legend(fontsize=8); ax[2,1].set_xticks(range(3),TASK_SIZES)
        ax[2,1].set_xlabel("请求大小")
        style(ax[2,0],"（戊）基础往返时延为 12 ms 时的拥塞窗口",BYTES)
        style(ax[2,1],"（己）基础往返时延为 12 ms 时的上传耗时",TIME)
    figure(out,"B1",fig)


def plot_c(out, summary, tasks):
    fig, ax = plt.subplots(3,2,figsize=(12,10),layout="constrained")
    ax = ax.flatten()
    names = ["reactive","oracle","80pct"]
    labels = ("超时后\n改用低档", "按完全正确的预测\n提前选档", "按约八成准确率的预测\n提前选档")
    for cap,offset in ((10,-.16),(30,.16)):
        rows = [summary["C1_"+case,str(cap)] for case in names]
        x=[i+offset for i in range(3)]
        color = COLORS[0 if cap==10 else 2]
        label = f"模拟空口容量为 {cap} Mbps"
        for a,field in ((ax[0],"on_time"),(ax[1],"correct20")):
            bars=a.bar(x,[int(r[field]) for r in rows],.29,color=color,label=label)
            a.bar_label(bars,fontsize=8,padding=2)
        sizes = [[int(r["size"])/1000 for r in tasks if r["case"]=="C1_"+case
                  and int(r["cap_mbps"])==cap and r["retry"]=="0"] for case in names]
        p50 = [median(group) for group in sizes]
        ax[4].errorbar(x,p50,yerr=[[m-min(g) for m,g in zip(p50,sizes)],
            [max(g)-m for m,g in zip(p50,sizes)]],fmt="o",color=color,capsize=4,label=label)
    for a,title in zip(ax[:2],["（甲）首次发送在 20 ms 内上传完成的请求数",
                               "（乙）首次按时上传且本地模型答对的请求数"]):
        a.set_xticks(range(3),labels); a.set_ylim(0,94)
        style(a,title,"请求数（每组共 72 个请求）")
    ax[0].legend(fontsize=8,loc="upper center",ncol=2)
    low = [summary["C1_"+name,"10"] for name in names]
    for a,field,title,unit,scale in ((ax[2],"upload_p50","（丙）低容量状态下的首次上传耗时中位数",TIME,1),
            (ax[3],"queue20_p50","（丁）低容量状态下的首次发送队列峰值中位数",BYTES,.001)):
        bars=a.bar(range(3),[float(r[field])*scale for r in low],color=COLORS[:3])
        a.bar_label(bars,fmt="%.1f",padding=2); a.margins(y=.2)
        a.set_xticks(range(3),labels)
        style(a,title,unit)
    ax[2].axhline(20,color="black",linestyle=":",label="20 ms 上传时限")
    ax[2].legend(fontsize=8,loc="upper right")
    style(ax[4],"（戊）各方案的首次请求大小",BYTES)
    ax[4].set_xticks(range(3),labels)
    ax[4].legend(fontsize=8)
    reactive = [r for r in tasks if r["case"]=="C1_reactive" and r["cap_mbps"]=="10"]
    p50 = [median(float(r["pacing_initial_mbps"]) for r in reactive if r["retry"]==str(i)) for i in range(2)]
    bars=ax[5].bar(range(2),p50,.5,color=(COLORS[0],COLORS[2]))
    ax[5].bar_label(bars,fmt="%.1f",padding=2)
    ax[5].set_xticks(range(2),["高档首次发送","低档重新发送"])
    ax[5].margins(y=.2)
    style(ax[5],"（己）超时后改用低档方案的起始目标发包速率中位数",RATE)
    figure(out,"C1",fig)


def plot_d(out, curves, summary):
    fig, ax = plt.subplots(3,2,figsize=(12,10),layout="constrained")
    names=["native","high","pattern","low","equal"]
    for a,field,unit,title,scale in ((ax[0,0],"pacing_mbps",RATE,"（甲）目标发包速率",1),
        (ax[0,1],"tx",RATE,"（乙）实际入队速率（每 2 ms 统计）",1),
        (ax[1,0],"queue",BYTES,"（丙）中继队列中的待发送数据量",.001),
        (ax[2,0],"cwnd_bytes",BYTES,"（戊）拥塞窗口",.001),
        (ax[2,1],"rx",RATE,"（己）实际出队速率（每 2 ms 统计）",1)):
        for idx,name in enumerate(names):
            if field in ("tx","rx"):
                intervals = defaultdict(int)
                for row in curves["D1_"+name,50000]:
                    start = int(float(row["ms"]))//2*2
                    intervals[row["repeat"],row["task"],start] += int(row[field])
                by_time = defaultdict(list)
                for (_,_,start),amount in intervals.items():
                    by_time[start].append(amount*8/2000)
                pts = [(start,median(values)) for start,values in sorted(by_time.items())]
            else:
                pts=aggregate(curves,"D1_"+name,50000,field)
            a.step([t for t,v in pts],[v*scale for t,v in pts],where="post",color=COLORS[idx],
                   label=PACING_NAMES[idx],linewidth=1.4)
        if field in ("pacing_mbps","tx","rx"):
            a.step([0,10,32],[30,15,15],where="post",color="black",linestyle=":",label="模拟空口容量")
        a.axvline(20,linestyle="--",color="gray",alpha=.65); a.set_xlim(0,32)
        a.set_xlabel(TASK_TIME)
        style(a,title,unit)
    handles, labels = ax[0,0].get_legend_handles_labels()
    fig.legend(handles, labels, fontsize=8, ncol=3, loc="outside upper center")
    a=ax[1,1]
    for i,name in enumerate(names):
        row=summary["D1_"+name,"50000"]
        a.plot([float(row["upload_p50"]),float(row["upload_p95"])],[i,i],color=COLORS[i],marker="o")
    a.set_yticks(range(len(names)),[name.replace("，", "，\n") for name in PACING_NAMES])
    a.axvline(20,color="gray",linestyle="--")
    a.set_xlabel("上传耗时（ms）")
    style(a,"（丁）上传耗时的中位数至第 95 百分位数","")
    figure(out,"D1",fig)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("out",type=Path)
    p.add_argument("--font",type=Path,help="简体中文字体文件")
    args=p.parse_args()
    set_chinese_font(args.font)
    out=args.out
    def rows(name):
        with (out/name).open() as file:
            return list(csv.DictReader(file))
    summary={(r["case"],r["group"]):r for r in rows("task-summary.csv")}
    runs=json.loads((out/"runs.json").read_text())
    curves=defaultdict(list)
    with gzip.open(out/"BD-curves.csv.gz","rt") as handle:
        for r in csv.DictReader(handle):
            curves[r["case"],int(r["size"])].append(r)
    with gzip.open(out/"tasks.csv.gz","rt") as handle:
        tasks=[r for r in csv.DictReader(handle) if r["case"].startswith("C1_")]
    plot_window_and_pacing(out)
    plot_a(out,runs,rows("A1-trace.csv"),summary)
    plot_a_slow(out)
    plot_b(out,curves,summary)
    plot_c(out,summary,tasks)
    plot_d(out,curves,summary)


if __name__ == "__main__":
    main()
