"""Анализ лица на фото через MediaPipe Face Mesh.

ВАЖНО: это эвристика для развлечения (пропорции, симметрия, наклон глаз и т.п.),
а не научная оценка внешности. Все пороги можно подкрутить ниже.
"""
import math
import threading

import cv2
import mediapipe as mp
import numpy as np

_lock = threading.Lock()
_mesh = None


class FaceError(Exception):
    """Фото не подходит для анализа."""


# ключ -> (название, формат значения)
METRIC_INFO = {
    "symmetry": ("Симметрия", "{:.1f}% асимм."),
    "tilt": ("Кантальный наклон", "{:+.1f}°"),
    "thirds": ("Трети лица", "откл. {:.2f}"),
    "fwhr": ("fWHR (ширина/высота)", "{:.2f}"),
    "jaw": ("Челюсть / скулы", "{:.2f}"),
    "eyes": ("Расстояние между глазами", "{:.2f}"),
    "mouth": ("Рот / нос", "{:.2f}"),
}

# вес каждой метрики в итоговой оценке
WEIGHTS = {
    "symmetry": 0.22,
    "tilt": 0.18,
    "thirds": 0.15,
    "fwhr": 0.12,
    "jaw": 0.13,
    "eyes": 0.10,
    "mouth": 0.10,
}


def _get_mesh():
    global _mesh
    if _mesh is None:
        _mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=True,
            max_num_faces=2,
            refine_landmarks=True,
            min_detection_confidence=0.5,
        )
    return _mesh


def _g(x: float, ideal: float, tol: float) -> float:
    """Колокол: 1.0 в идеале, падает при отклонении."""
    return math.exp(-(((x - ideal) / tol) ** 2))


def analyze(raw: bytes) -> dict:
    arr = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise FaceError("Не удалось открыть фото.")
    h, w = img.shape[:2]
    side = max(h, w)
    if side > 1280:
        k = 1280 / side
        img = cv2.resize(img, (int(w * k), int(h * k)))
        h, w = img.shape[:2]

    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    with _lock:
        res = _get_mesh().process(rgb)
    faces = res.multi_face_landmarks or []
    if not faces:
        raise FaceError("Лицо не найдено. Нужно чёткое фото анфас при хорошем свете.")
    if len(faces) > 1:
        raise FaceError("На фото должно быть только одно лицо.")

    P = np.array([[p.x * w, p.y * h] for p in faces[0].landmark])

    def d(a, b):
        return float(np.linalg.norm(P[a] - P[b]))

    face_w = d(234, 454)
    if face_w < 40:
        raise FaceError("Лицо слишком маленькое на фото. Подойди ближе.")

    # поворот головы
    dl, dr = d(1, 234), d(1, 454)
    if abs(dl - dr) / (dl + dr) > 0.22:
        raise FaceError("Повернись лицом прямо к камере (анфас).")

    # --- симметрия относительно средней линии лица (10 -> 152)
    top, chin = P[10], P[152]
    axis = chin - top
    axis_len = float(np.linalg.norm(axis))

    def side_dist(i):
        v = P[i] - top
        return (axis[0] * v[1] - axis[1] * v[0]) / axis_len

    pairs = [(33, 263), (133, 362), (61, 291), (129, 358), (234, 454),
             (172, 397), (70, 300), (105, 334), (93, 323), (132, 361)]
    err = float(np.mean([abs(abs(side_dist(a)) - abs(side_dist(b))) / face_w for a, b in pairs]))
    s_sym = _g(err, 0.0, 0.045)

    # --- кантальный наклон (внешний уголок глаза выше внутреннего = плюс)
    def tilt(inner, outer):
        dy = P[inner][1] - P[outer][1]
        dx = abs(P[outer][0] - P[inner][0])
        return math.degrees(math.atan2(dy, dx))

    t = (tilt(133, 33) + tilt(362, 263)) / 2
    s_tilt = _g(t, 5.0, 6.0)

    # --- трети лица
    parts = [d(10, 168), d(168, 2), d(2, 152)]
    dev = float(np.std(parts) / np.mean(parts))
    s_thirds = _g(dev, 0.0, 0.18)

    # --- fWHR
    fwhr = face_w / max(d(168, 0), 1e-6)
    s_fwhr = _g(fwhr, 1.95, 0.35)

    # --- челюсть относительно скул
    jaw = d(172, 397) / face_w
    s_jaw = _g(jaw, 0.80, 0.12)

    # --- расстояние между глазами / ширина глаза
    eye_w = (d(33, 133) + d(263, 362)) / 2
    eyes = d(133, 362) / max(eye_w, 1e-6)
    s_eyes = _g(eyes, 1.0, 0.22)

    # --- рот / нос
    mouth = d(61, 291) / max(d(129, 358), 1e-6)
    s_mouth = _g(mouth, 1.5, 0.30)

    metrics = {
        "symmetry": {"value": err * 100, "score": s_sym},
        "tilt": {"value": t, "score": s_tilt},
        "thirds": {"value": dev, "score": s_thirds},
        "fwhr": {"value": fwhr, "score": s_fwhr},
        "jaw": {"value": jaw, "score": s_jaw},
        "eyes": {"value": eyes, "score": s_eyes},
        "mouth": {"value": mouth, "score": s_mouth},
    }
    m = sum(WEIGHTS[k] * v["score"] for k, v in metrics.items())
    # растягиваем 0.15..0.80 в 0..5
    score = 5 * min(1.0, max(0.0, (m - 0.15) / 0.65))
    return {"score": round(score, 2), "metrics": metrics}
