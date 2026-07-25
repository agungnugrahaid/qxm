"""
charts.py -- native matplotlib charts for the monthly PDF report.

Replaces the Grafana image-renderer panels (headless Chrome, ~7 s each) with
charts drawn straight from TimescaleDB rows in-process (~0.1 s each). Each
function returns a PNG data: URI, so report_lib can drop it into the same
template image slot the Grafana PNGs used (report_template.html {{ s.png_uri }}).

Styled once to the Lumina Console tokens (admin-ui/DESIGN.md) + Montserrat (the
report typeface, TTFs vendored in assets/fonts/), so the charts match the rest
of the document. Agg backend -- no display, safe in a container.
"""

import base64
import io
import os

import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager, dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

# --- Montserrat (fall back to sans-serif if the TTFs aren't found) -----------
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
for _f in ("Montserrat-Regular.ttf", "Montserrat-SemiBold.ttf", "Montserrat-Bold.ttf"):
    _p = os.path.join(_FONTS_DIR, _f)
    if os.path.exists(_p):
        font_manager.fontManager.addfont(_p)
_HAS_MONT = any("Montserrat" in f.name for f in font_manager.fontManager.ttflist)

# --- Lumina tokens -----------------------------------------------------------
_INK = "#131c28"
_MUTED = "#434751"
_GRID = "#dae3f4"
_OUTLINE = "#c3c6d2"
# Categorical series palette (blues first, warm/red last for "bad" metrics).
PALETTE = ["#1e4b8f", "#00639b", "#345da2", "#70bcff", "#882f0a", "#ba1a1a", "#737782"]

plt.rcParams.update({
    "font.family": "Montserrat" if _HAS_MONT else "sans-serif",
    "font.size": 9,
    "text.color": _MUTED,
    "axes.edgecolor": _OUTLINE,
    "axes.labelcolor": _MUTED,
    "axes.titlecolor": _INK,
    "xtick.color": _MUTED,
    "ytick.color": _MUTED,
    "axes.grid": True,
    "grid.color": _GRID,
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 120,
    "savefig.dpi": 120,
})

_FIGSIZE = (10.0, 3.2)   # wide, ~A4 content aspect; template scales to 100% width


def _nan(seq):
    """None -> NaN so matplotlib breaks the line across gap-filled holes
    instead of drawing down to zero."""
    return np.array([np.nan if v is None else float(v) for v in seq], dtype=float)


def _all_nan(arr):
    return arr.size == 0 or bool(np.all(np.isnan(arr)))


def _timeaxis(ax, times):
    """Date x-axis: day/month ticks for a multi-day report, no clutter."""
    if not times:
        return
    ax.set_xlim(times[0], times[-1])
    span_days = (times[-1] - times[0]).total_seconds() / 86400 if len(times) > 1 else 1
    if span_days > 3:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=9))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))


def _finish(fig):
    fig.tight_layout(pad=0.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _empty(msg="No data for this period"):
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.axis("off")
    ax.text(0.5, 0.5, msg, ha="center", va="center", color=_MUTED, fontsize=11)
    return _finish(fig)


def line_chart(times, series, y_label=None, y_suffix="", y_max=None):
    """Multi-series time line. `series` = {label: [values|None]}. Used for
    latency/jitter/loss, CPU/RAM/Disk, and (single-series) client count."""
    series = {k: _nan(v) for k, v in series.items() if not _all_nan(_nan(v))}
    if not times or not series:
        return _empty()
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    for i, (label, ys) in enumerate(series.items()):
        ax.plot(times, ys, label=label, color=PALETTE[i % len(PALETTE)],
                linewidth=1.4, solid_capstyle="round")
    if y_label:
        ax.set_ylabel(y_label)
    ax.margins(x=0)
    ax.set_ylim(bottom=0)
    if y_max is not None:
        ax.set_ylim(top=y_max)
    if y_suffix:
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}{y_suffix}"))
    _timeaxis(ax, times)
    # Legend only when it adds information (more than one series).
    if len(series) > 1:
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=min(len(series), 4),
                  frameon=False, fontsize=8, handlelength=1.4, columnspacing=1.4)
    return _finish(fig)


def area_updown_chart(times, download_bps, upload_bps, y_label="Throughput"):
    """Filled download/upload area for the aggregate uplink traffic panel.
    Inputs are bits/sec; the axis auto-scales to Kbps/Mbps/Gbps."""
    down = _nan(download_bps)
    up = _nan(upload_bps)
    if not times or (_all_nan(down) and _all_nan(up)):
        return _empty()
    peak = np.nanmax(np.concatenate([down, up])) if (down.size or up.size) else 0
    peak = 0 if np.isnan(peak) else peak
    unit, div = ("Gbps", 1e9) if peak >= 1e9 else ("Mbps", 1e6) if peak >= 1e6 else ("Kbps", 1e3)
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.fill_between(times, 0, down / div, color=PALETTE[0], alpha=0.30, linewidth=0)
    ax.plot(times, down / div, color=PALETTE[0], linewidth=1.4, label="Download")
    ax.fill_between(times, 0, up / div, color=PALETTE[1], alpha=0.22, linewidth=0)
    ax.plot(times, up / div, color=PALETTE[1], linewidth=1.4, label="Upload")
    ax.set_ylabel(f"{y_label} ({unit})")
    ax.set_ylim(bottom=0)
    ax.margins(x=0)
    _timeaxis(ax, times)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2,
              frameon=False, fontsize=8, handlelength=1.4)
    return _finish(fig)


def hbar_chart(labels, values, value_labels=None):
    """Horizontal bar chart (top internal users by traffic). Biggest on top."""
    if not labels:
        return _empty()
    order = list(range(len(labels)))[::-1]   # matplotlib draws bottom-up
    y = np.arange(len(labels))
    h = max(2.2, 0.34 * len(labels) + 0.8)
    fig, ax = plt.subplots(figsize=(_FIGSIZE[0], h))
    ax.barh([y[i] for i in range(len(labels))], [values[i] for i in range(len(labels))],
            color=PALETTE[0], height=0.66)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()   # first row (biggest) at the top
    ax.grid(axis="y", visible=False)
    ax.spines["left"].set_visible(False)
    vmax = max(values) if values else 1
    if value_labels:
        for i, vl in enumerate(value_labels):
            ax.text(values[i] + vmax * 0.01, y[i], vl, va="center", fontsize=7.5, color=_MUTED)
    ax.set_xlim(0, vmax * 1.18)
    ax.xaxis.set_visible(False)
    return _finish(fig)


def dual_axis_chart(times, signal, satisfaction, retry_pct):
    """Wi-Fi quality: signal (dBm, left axis) + satisfaction% and retry% (right
    axis). Three series, mixed units -- the fiddliest panel."""
    sig = _nan(signal)
    sat = _nan(satisfaction)
    ret = _nan(retry_pct)
    if not times or (_all_nan(sig) and _all_nan(sat) and _all_nan(ret)):
        return _empty()
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    lines = []
    if not _all_nan(sig):
        lines += ax.plot(times, sig, color=PALETTE[0], linewidth=1.4, label="Signal (dBm)")
    ax.set_ylabel("Signal (dBm)")
    ax.margins(x=0)
    ax2 = ax.twinx()
    ax2.grid(False)
    ax2.spines["top"].set_visible(False)
    if not _all_nan(sat):
        lines += ax2.plot(times, sat, color=PALETTE[1], linewidth=1.4, label="Satisfaction (%)")
    if not _all_nan(ret):
        lines += ax2.plot(times, ret, color=PALETTE[5], linewidth=1.4, label="Retry (%)")
    ax2.set_ylabel("Percent")
    ax2.set_ylim(0, 100)
    _timeaxis(ax, times)
    if lines:
        ax.legend(lines, [ln.get_label() for ln in lines], loc="upper center",
                  bbox_to_anchor=(0.5, -0.18), ncol=3, frameon=False, fontsize=8,
                  handlelength=1.4, columnspacing=1.4)
    return _finish(fig)
