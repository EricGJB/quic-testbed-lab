"""根据第三版实验数据生成中文图表。"""

import argparse
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

COLORS = ["#657186", "#968153", "#147b8e", "#b65e43", "#78498e"]
RATE = "速率（兆比特/秒）"
BYTES = "数据量（千字节）"
TIME = "时间（毫秒）"
TASK_TIME = "从提交请求开始计时（毫秒）"
TASK_SIZES = ["40 千字节", "200 千字节", "1 兆字节"]
PIPE_NAMES = ("普通管道（短时波动）", "恒定速率管道", "稳定高容量管道", "普通管道（长时波动）")
GRANT_NAMES = ("固定分配 20 兆比特/秒", "容量在 10 和 30 兆比特/秒之间切换",
               "按需分配，发送端自行调节", "按需分配，发送端提前适配")
PACING_NAMES = ("由拥塞控制算法自行调节", "固定使用 28 兆比特/秒",
                "先用 28 兆比特/秒，后用 14 兆比特/秒",
                "固定使用 14 兆比特/秒", "固定使用 21 兆比特/秒")


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
    ax[1,0].set_xlabel("从实验计时起点开始计时（秒）")
    ax[1,0].legend(fontsize=8,ncol=2)
    periodic = [summary["A1_"+case,"all"] for case in names[:3]]
    bars = ax[1,1].bar(range(3),[100*int(r["on_time"])/int(r["tasks"]) for r in periodic],color=COLORS[:3])
    ax[1,1].bar_label(bars,fmt="%.1f%%")
    ax[1,1].set_xticks(range(3),labels[:3]); ax[1,1].set_ylim(0,113)
    style(ax[1,1],"（丁）每 20 毫秒提交 40 千字节请求的上传表现","20 毫秒内上传完成的请求占比（%）")
    tail = ax[1,1].twinx()
    tail.plot(range(3),[float(r["token_p99"]) for r in periodic],"k:o",label="传输回执耗时的第 99 百分位数")
    tail.set_ylabel("传输回执耗时的第 99 百分位数（毫秒）")
    tail.set_ylim(0,32)
    tail.spines["top"].set_visible(False)
    tail.legend(loc="lower right",fontsize=8)
    figure(out,"A1",fig)


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
    ax[0,0].set_xticks(range(3),[f"{size}\n时限 {budget} 毫秒" for size,budget in zip(TASK_SIZES,(24,70,310))])
    ax[0,0].legend(fontsize=8,ncol=2,loc="upper center")
    ax[0,0].set_ylim(0,137); ax[0,0].set_yticks(range(0,101,20))
    ax[0,0].set_xlabel("请求大小及对应的补测时限")
    style(ax[0,0],"（甲）补测时限内的上传完成率","按时完成的请求占比（%）")
    for a,field,unit,title,scale in ((ax[0,1],"pacing_mbps",RATE,"（乙）200 千字节请求的目标发包速率",1),
        (ax[1,0],"cwnd_bytes",BYTES,"（丙）200 千字节请求的拥塞窗口",.001),
        (ax[1,1],"queue",BYTES,"（丁）200 千字节请求的中继队列",.001)):
        for idx,name in enumerate(names[2:],2):
            pts=aggregate(curves,"B1_"+name,200000,field)
            a.plot([t for t,v in pts if t<=30],[v*scale for t,v in pts if t<=30],color=COLORS[idx],label=GRANT_NAMES[idx])
        if field == "pacing_mbps": a.axhline(30,color="black",linestyle=":",label="网络分配速率为 30 兆比特/秒")
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
        style(ax[2,0],"（戊）基础往返时延为 12 毫秒时的拥塞窗口",BYTES)
        style(ax[2,1],"（己）基础往返时延为 12 毫秒时的上传耗时",TIME)
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
        label = f"模拟空口容量为 {cap} 兆比特/秒"
        for a,field in ((ax[0],"on_time"),(ax[1],"correct20")):
            bars=a.bar(x,[int(r[field]) for r in rows],.29,color=color,label=label)
            a.bar_label(bars,fontsize=8,padding=2)
        sizes = [[int(r["size"])/1000 for r in tasks if r["case"]=="C1_"+case
                  and int(r["cap_mbps"])==cap and r["retry"]=="0"] for case in names]
        p50 = [median(group) for group in sizes]
        ax[4].errorbar(x,p50,yerr=[[m-min(g) for m,g in zip(p50,sizes)],
            [max(g)-m for m,g in zip(p50,sizes)]],fmt="o",color=color,capsize=4,label=label)
    for a,title in zip(ax[:2],["（甲）首次发送在 20 毫秒内上传完成的请求数",
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
    ax[2].axhline(20,color="black",linestyle=":",label="20 毫秒上传时限")
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
        (ax[0,1],"tx",RATE,"（乙）实际入队速率（每 2 毫秒统计）",1),
        (ax[1,0],"queue",BYTES,"（丙）中继队列中的待发送数据量",.001),
        (ax[2,0],"cwnd_bytes",BYTES,"（戊）拥塞窗口",.001),
        (ax[2,1],"rx",RATE,"（己）实际出队速率（每 2 毫秒统计）",1)):
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
    a.set_xlabel("上传耗时（毫秒）")
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
    plot_a(out,runs,rows("A1-trace.csv"),summary)
    plot_b(out,curves,summary)
    plot_c(out,summary,tasks)
    plot_d(out,curves,summary)


if __name__ == "__main__":
    main()
