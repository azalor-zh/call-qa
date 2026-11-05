"""Разделение голосов в моно-записи по акустике, без нейросетей (только numpy).

Каждая реплика описывается усреднённым спектром голоса (12 кепстральных коэффициентов, как в MFCC); реплики делятся на два голоса
взвешенным k-means. Если голоса разделяются плохо (похожие голоса, шум, один говорящий), возвращается None, и роли расставляются
как раньше — по смыслу разговора, моделью. Метки "A"/"B" потом получают роли через ту же схему, что и стерео-каналы.
Основной тон (F0) пробовали, но на телефонных записях он шумный и точность не повышает, поэтому не используется.
Метод простой и не заменяет нейросетевую диаризацию. Замер на 12 стерео-записях пилота (моно-микс против эталона по каналам,
bench/diarize_eval.py): точность на записях с ≥ 30 репликами ≈ 0.91 при доле большего голоса ≈ 0.66; на одной из восьми запись
разделилась неверно (0.53). Подробности — docs/15-backlog.md.
"""
import numpy as np

RATE = 16000
FRAME, HOP = 400, 160  # 25 мс и 10 мс
EDGES = np.geomspace(100, 3800, 25)  # 24 полосы, пригодные и для телефонного звука
N_CEPS = 12
MIN_SPANS = 30  # на коротких записях (меньше реплик) кластеры случайны, там остаётся разметка по смыслу
MIN_SEP = 0.8  # расстояние между голосами в долях разброса внутри голоса; порог выбран по замеру (на нём отсекается только мусор, не точность)
MIN_SHARE = 0.2  # доля речи минимум у каждого из двух голосов
_WIN = np.hanning(FRAME).astype(np.float32)
_BAND_IDX = np.digitize(np.fft.rfftfreq(FRAME, 1 / RATE), EDGES) - 1
_K = np.arange(1, N_CEPS + 1)[:, None]
_DCT = np.cos(np.pi * _K * (np.arange(len(EDGES) - 1)[None, :] + 0.5) / (len(EDGES) - 1))


def _frames(x: np.ndarray) -> np.ndarray:
    n = 1 + (len(x) - FRAME) // HOP
    if n < 5:
        return np.empty((0, FRAME), np.float32)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    return x[idx].astype(np.float32)


def features(x: np.ndarray):
    """Кепстр одной реплики (вектор из 12 чисел) либо None, если речи в реплике нет."""
    fr = _frames(x[: 10 * RATE])
    if not len(fr):
        return None
    rms = np.sqrt((fr ** 2).mean(axis=1))
    voiced = fr[rms > 0.35 * np.percentile(rms, 90)]
    if len(voiced) < 5:
        return None
    power = (np.abs(np.fft.rfft(voiced * _WIN, axis=1)) ** 2).mean(axis=0) + 1e-6
    bands = np.array([np.log(power[_BAND_IDX == k].mean() + 1e-6) if (_BAND_IDX == k).any() else 0.0 for k in range(len(EDGES) - 1)])
    return _DCT @ bands  # коэффициенты от 1-го: нулевой (громкость записи) отброшен


def _kmeans2(z: np.ndarray, w: np.ndarray, rng) -> tuple:
    best = None
    for _ in range(12):
        c = z[rng.choice(len(z), 2, replace=False)].copy()
        for _ in range(60):
            d = ((z[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
            lab = d.argmin(axis=1)
            new = np.array([(z[lab == k] * w[lab == k, None]).sum(axis=0) / max(w[lab == k].sum(), 1e-9) if (lab == k).any() else c[k] for k in range(2)])
            if np.allclose(new, c):
                break
            c = new
        cost = float((((z - c[lab]) ** 2).sum(axis=1) * w).sum())
        if best is None or cost < best[0]:
            best = (cost, lab, c)
    return best[1], best[2]


def assign(x: np.ndarray, spans: list, report: dict = None):
    """Метки "A"/"B" для каждой реплики (spans — (начало, конец) в отсчётах) либо None, если голоса не разделились.
    report, если передан, получает показатели разделения (sep, share) — для отладки и замеров."""
    feats = [features(x[a:b]) for a, b in spans]
    ok = [i for i, f in enumerate(feats) if f is not None]
    if len(ok) < MIN_SPANS:
        return None
    c12 = np.array([feats[i] for i in ok])
    w = np.array([min(spans[i][1] - spans[i][0], 10 * RATE) / RATE for i in ok], dtype=float)
    z = (c12 - c12.mean(axis=0)) / (c12.std(axis=0) + 1e-6) / np.sqrt(N_CEPS) * 3.0
    lab, c = _kmeans2(z, w, np.random.default_rng(1))
    share = [float(w[lab == k].sum() / w.sum()) for k in range(2)]
    within = float((((z - c[lab]) ** 2).sum(axis=1) * w).sum() / w.sum())
    sep = float(((c[0] - c[1]) ** 2).sum() / max(within, 1e-9))
    if report is not None:
        report.update(sep=sep, share=min(share))
    if min(share) < MIN_SHARE or sep < MIN_SEP:
        return None
    out = [None] * len(spans)
    for j, i in enumerate(ok):
        out[i] = "AB"[int(lab[j])]
    for i in range(len(spans)):  # реплики без признаков (короткие, тихие) — к ближайшей по времени размеченной
        if out[i] is None:
            out[i] = out[min(ok, key=lambda k: abs(k - i))]
    return out
