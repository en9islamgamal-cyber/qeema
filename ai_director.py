"""
ai_director.py
AI Director: مسؤول عن تحديد لحظات التفاعل (encouragement events)
"""

import numpy as np
from smart_timing import SmartTimingEngine


class AIDirector:
    def __init__(self):
        self.timing_engine = SmartTimingEngine()

    def analyze_audio(self, video_path, duration):
        """
        تحليل الصوت واستخراج:
        - peaks (لحظات قوية)
        - silence (لحظات هدوء)
        """
        wav = self.timing_engine.extract_audio(video_path)
        signal = self.timing_engine.load_wav(wav)

        energies = self.timing_engine.compute_energy(signal)

        # Normalize
        energies = (energies - energies.min()) / (energies.ptp() + 1e-6)

        # Smooth
        kernel = np.ones(10) / 10
        smooth = np.convolve(energies, kernel, mode="same")

        # Peaks detection
        peaks = np.where(smooth > 0.7)[0]

        # Silence detection
        silence = np.where(smooth < 0.2)[0]

        return {
            "smooth": smooth,
            "peaks": peaks,
            "silence": silence,
            "length": len(smooth),
            "duration": duration
        }

    def decide_events(self, video_path, duration):
        """
        تحديد نقاط التفاعل الذكية
        """
        data = self.analyze_audio(video_path, duration)

        events = []
        used_times = []

        for idx in data["peaks"]:
            time_sec = (idx / data["length"]) * duration

            # فلترة:
            if time_sec < 3 or time_sec > duration - 3:
                continue

            # منع التكرار القريب
            if any(abs(time_sec - t) < 5 for t in used_times):
                continue

            events.append({
                "time": round(time_sec, 2),
                "type": "encourage"
            })

            used_times.append(time_sec)

            # نحدد أقصى عدد أحداث
            if len(events) >= 3:
                break

        # fallback لو مفيش حاجة
        if not events:
            events.append({
                "time": duration * 0.5,
                "type": "encourage"
            })

        return events