import subprocess as sp
import tempfile
import numpy as np
import wave
import contextlib

class SmartTimingEngine:
    """
    AI-like engine لتحديد أفضل لحظة لعرض النص
    يعتمد على تحليل الصوت + الصمت + الإيقاع
    """

    def __init__(self):
        self.sample_rate = 16000

    def extract_audio(self, video_path):
        """تحويل الفيديو إلى WAV مؤقت"""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()

        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-vn",
            tmp.name
        ]
        sp.run(cmd, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        return tmp.name

    def load_wav(self, wav_path):
        with contextlib.closing(wave.open(wav_path, 'r')) as f:
            frames = f.readframes(f.getnframes())
            signal = np.frombuffer(frames, dtype=np.int16)
            return signal.astype(np.float32)

    def compute_energy(self, signal, frame_size=1024):
        """حساب طاقة الصوت لكل frame"""
        energies = []
        for i in range(0, len(signal), frame_size):
            frame = signal[i:i+frame_size]
            if len(frame) == 0:
                continue
            energy = np.sum(frame**2) / len(frame)
            energies.append(energy)
        return np.array(energies)

    def find_best_moment(self, video_path, duration):
        wav = self.extract_audio(video_path)
        signal = self.load_wav(wav)

        energies = self.compute_energy(signal)

        # Normalize
        energies = (energies - energies.min()) / (energies.ptp() + 1e-6)

        # Smooth
        kernel = np.ones(10) / 10
        smooth = np.convolve(energies, kernel, mode="same")

        # نحسب score:
        # - نريد طاقة متوسطة (مش صمت ولا صراخ)
        target = 0.6
        score = 1 - np.abs(smooth - target)

        best_idx = np.argmax(score)

        # تحويل index → time
        time = (best_idx / len(smooth)) * duration

        # نتجنب أول وآخر الفيديو
        time = max(3, min(duration - 3, time))

        return time