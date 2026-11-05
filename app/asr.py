"""Распознавание: GigaAM v3 на CPU (видеокарты заняты основной моделью, см. docs/13).

Запись → 16 кГц → реплики, нарезанные по паузам → текст с метками времени.
Стерео с разными каналами (запись АТС по каналам) распознаётся по каналам: роль определяется каналом.
Моно режется на реплики, роли расставляет scoring.label_speakers по смыслу разговора.
Один экземпляр модели на процесс, вызовы сериализуются блокировкой.
"""
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np

RATE = 16000
_lock = threading.Lock()
_model = None


class Stopped(Exception):
    """Пользователь остановил обработку."""


def _ffmpeg() -> str:
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not Path(exe).is_file():  # в службе нет нужного окружения — берём бинарник из пакета напрямую
        exe = str(next((Path(imageio_ffmpeg.__file__).parent / "binaries").glob("ffmpeg-*")))
    return exe


def compute_peaks(src: Path, n: int = 190) -> list:
    """Амплитуда для звуковой дорожки: n значений 0..1 (по пикам в окнах)."""
    out = subprocess.run([_ffmpeg(), "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.abs(np.frombuffer(out, "<i2").astype(np.float32))
    if not len(x):
        return [0.0] * n
    p = np.array([a.max() if len(a) else 0 for a in np.array_split(x, n)])
    p = (p / (p.max() or 1)) ** 0.7
    return [round(float(v), 3) for v in p]


# Где считаем: процессор сервера, видеокарта сервера или внешний API. Whisper медленнее GigaAM на процессоре, но понимает казахский.
DEVICES = {"cpu": "Процессор сервера", "gpu": "Видеокарта сервера", "api": "Внешний API"}
ENGINES = {
    "cpu": {
        "gigaam-rnnt": "GigaAM v3 RNNT — русский, быстрее всего (≈ 3 с на минуту записи)",
        "gigaam-ctc": "GigaAM v3 CTC — русский, чуть проще и быстрее RNNT",
        "whisper-large-v3-turbo": "Whisper large-v3-turbo — русский и казахский, медленно (≈ 2 мин на минуту записи)",
        "whisper-large-v3": "Whisper large-v3 — русский и казахский, ещё медленнее (не проверялся в пилоте)",
    },
    "gpu": {  # GigaAM на видеокарте не включаем: в окружении службы torch собран только для процессора
        "whisper-large-v3-turbo": "Whisper large-v3-turbo — русский и казахский (≈ 2 с на минуту записи, 1,3–2,3 ГБ видеопамяти)",
        "whisper-large-v3": "Whisper large-v3 — русский и казахский (≈ 4,5 с на минуту записи, 4,1 ГБ видеопамяти)",
    },
}
CONFIG = {"engine": "gigaam-rnnt", "whisper_lang": "ru", "device": "cpu", "api_url": "", "api_key": "", "api_model": "whisper-1"}  # задаётся из настроек администратора перед каждым звонком


def engine_label() -> str:
    """Что записать в журнал звонка."""
    if CONFIG["device"] == "api":
        return f"API · {CONFIG['api_model']}"
    return CONFIG["engine"] + (" · GPU" if CONFIG["device"] == "gpu" else "")


def _load(engine: str, device: str):
    """Держим в памяти один движок: при смене выгружаем прежний."""
    global _model
    key = (engine, device)
    if _model is None or _model[0] != key:
        _model = None
        if engine.startswith("gigaam"):
            import gigaam
            import torch
            torch.set_num_threads(8)
            _model = (key, gigaam.load_model("v3_e2e_" + engine.split("-")[1], device="cpu", fp16_encoder=False))
        else:
            from faster_whisper import WhisperModel
            gpu = device == "gpu"
            _model = (key, WhisperModel(engine.removeprefix("whisper-"), device="cuda" if gpu else "cpu", cpu_threads=8,
                                        compute_type="int8_float16" if gpu else "int8"))
    return _model[1]


def _api_transcribe(wav: Path) -> str:
    """OpenAI-совместимый /audio/transcriptions (OpenAI, Groq, свой сервер faster-whisper-server и т. п.)."""
    import httpx
    data = {"model": CONFIG["api_model"], "response_format": "json"}
    if CONFIG["whisper_lang"] != "auto":
        data["language"] = CONFIG["whisper_lang"]
    with wav.open("rb") as f:
        r = httpx.post(f"{CONFIG['api_url'].rstrip('/')}/audio/transcriptions", headers={"Authorization": f"Bearer {CONFIG['api_key']}"},
                       data=data, files={"file": (wav.name, f, "audio/wav")}, timeout=300)
    r.raise_for_status()
    return r.json()["text"].strip()


def decode(src: Path) -> np.ndarray:
    """Весь файл как массив (отсчёты, 2) float32 на 16 кГц; у моно оба канала одинаковые."""
    out = subprocess.run([_ffmpeg(), "-loglevel", "error", "-i", str(src), "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, "<i2").astype(np.float32).reshape(-1, 2)


def is_stereo(x2: np.ndarray) -> bool:
    """Стерео по каналам — если каналы заметно различаются и оба содержат речь."""
    left, right = x2[:, 0], x2[:, 1]
    rl, rr = float(np.sqrt((left ** 2).mean() + 1e-9)), float(np.sqrt((right ** 2).mean() + 1e-9))
    diff = float(np.sqrt(((left - right) ** 2).mean()))
    return diff > 0.2 * max(rl, rr) and min(rl, rr) > 0.05 * max(rl, rr)


def split_utterances(x: np.ndarray, max_len: float = 20, min_len: float = 1.2, min_gap: float = 0.36) -> list:
    """Реплики по паузам: список (начало, конец) в отсчётах."""
    fr = int(0.03 * RATE)
    n = len(x) // fr
    if n == 0:
        return []
    e = np.abs(x[:n * fr]).reshape(n, fr).mean(axis=1)
    hi = float(np.percentile(e, 95))
    if hi < 30:  # тишина
        return []
    silent = e < max(float(np.percentile(e, 10)) * 3, 0.06 * hi)
    gap, cuts, i = int(min_gap / 0.03), [], 0
    while i < n:
        if silent[i]:
            j = i
            while j < n and silent[j]:
                j += 1
            if j - i >= gap:
                cuts.append((i + j) // 2 * fr)
            i = j
        else:
            i += 1
    bounds = [0] + cuts + [len(x)]
    merged = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if merged and (b - a < min_len * RATE or merged[-1][1] - merged[-1][0] < min_len * RATE):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    out, win = [], int(0.05 * RATE)
    for a, b in merged:
        while b - a > max_len * RATE:  # слишком длинную режем по самому тихому месту
            lo, up = a + 12 * RATE, a + int(max_len * RATE)
            energy = [np.abs(x[k:k + win]).mean() for k in range(lo, up, win)]
            cut = lo + int(np.argmin(energy)) * win
            out.append((a, cut))
            a = cut
        out.append((a, b))
    return out



def _text(model, x: np.ndarray, a: int, b: int, workdir: Path, tag: str, whisper: bool) -> str:
    """Текст одной реплики x[a:b] выбранным движком."""
    if whisper and CONFIG["device"] != "api":
        lang = CONFIG["whisper_lang"] if CONFIG["whisper_lang"] != "auto" else None
        ws, _ = model.transcribe(np.clip(x[a:b], -32768, 32767) / 32768, language=lang, beam_size=5, vad_filter=False,
                                 condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in ws).strip()
    chunk = workdir / f"{tag}{a}.wav"
    with wave.open(str(chunk), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.clip(x[a:b], -32768, 32767).astype("<i2").tobytes())
    try:
        if CONFIG["device"] == "api":
            return _api_transcribe(chunk)
        res = model.transcribe(str(chunk))  # gigaam 0.2: объект с полем text, раньше — строка
        return str(getattr(res, "text", res)).strip()
    finally:
        chunk.unlink(missing_ok=True)


def _recognize(model, x: np.ndarray, parts: list, workdir: Path, tag: str, should_stop=None, tick=None) -> list:
    """Реплики с текстом. Поля _a/_b (границы в отсчётах) нужны разделению голосов; transcribe() их убирает."""
    segs = []
    whisper = CONFIG["device"] != "api" and CONFIG["engine"].startswith("whisper")
    for a, b in parts:
        if should_stop and should_stop():  # проверка между репликами: остановка срабатывает за секунды
            raise Stopped()
        if tick:
            tick(b - a)  # прогресс считается по длине уже обработанного звука
        if b - a < 0.4 * RATE or float(np.abs(x[a:b]).mean()) < 25:  # короткое или тишина (иначе модель выдумывает слова)
            continue
        text = _text(model, x, a, b, workdir, tag, whisper)
        if text:
            segs.append({"t": round(a / RATE, 1), "text": text, "_a": a, "_b": b})
    return segs


def transcribe(src: Path, workdir: Path, should_stop=None, max_sec: int = 0, progress=None, diarize_mono: bool = False) -> dict:
    """Возвращает {"duration": сек, "stereo": bool, "voices": bool, "segments": [{"t": сек, "text": str, "ch": "A"|"B" (стерео или разделение голосов)}]}.
    max_sec > 0 — лимит длины записи: более длинная отклоняется до распознавания. should_stop() → True прерывает работу (Stopped).
    progress(доля 0..1) вызывается по ходу распознавания. diarize_mono — в моно разделить голоса по акустике (diarize.py); voices=True, если получилось."""
    x2 = decode(src)
    if max_sec and len(x2) / RATE > max_sec:
        raise ValueError(f"Запись длиннее лимита: {len(x2) / RATE / 60:.0f} мин при лимите {max_sec // 60} мин (лимит меняет администратор в разделе «Модели»)")
    stereo = is_stereo(x2)
    voices = False
    chans = [x2[:, 0], x2[:, 1]] if stereo else [x2.mean(axis=1)]
    plans = [split_utterances(x) for x in chans]
    total = max(sum(b - a for p in plans for a, b in p), 1)
    done = [0]

    def tick(n):
        done[0] += n
        if progress:
            progress(min(done[0] / total, 1.0))

    with _lock:
        model = None if CONFIG["device"] == "api" else _load(CONFIG["engine"], CONFIG["device"])
        segments = []
        for k, (x, parts) in enumerate(zip(chans, plans)):
            tag = "AB"[k] if stereo else "m"
            for s in _recognize(model, x, parts, workdir, tag, should_stop, tick):
                if stereo:
                    s["ch"] = tag
                segments.append(s)
        segments.sort(key=lambda s: s["t"])
    if not stereo and diarize_mono and segments:
        import diarize
        labels = diarize.assign(chans[0], [(s["_a"], s["_b"]) for s in segments])
        if labels:
            voices = True
            for s, lab in zip(segments, labels):
                s["ch"] = lab
    for s in segments:
        s.pop("_a", None)
        s.pop("_b", None)
    return {"duration": round(len(x2) / RATE), "stereo": stereo, "voices": voices, "segments": segments}
