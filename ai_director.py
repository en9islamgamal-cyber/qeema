"""
ai_director.py — VALUE / QEEMA v6.0
AI Director: مسؤول عن تحديد لحظات التفاعل بدقة سينمائية.
يعتمد على تحليل الوقفات الطبيعية (Silences) لعدم تشتيت المشاهد أثناء السرد.
"""

import numpy as np
from smart_timing import SmartTimingEngine
import logging

logger = logging.getLogger(__name__)

class AIDirector:
    def __init__(self):
        self.timing_engine = SmartTimingEngine()

    def analyze_audio(self, video_path: str, duration: float) -> dict:
        """تحليل الصوت واستخراج الطاقة لتقييم الوقفات."""
        try:
            wav = self.timing_engine.extract_audio(video_path)
            signal = self.timing_engine.load_wav(wav)
            energies = self.timing_engine.compute_energy(signal)

            # Normalize
            energies = (energies - energies.min()) / (energies.ptp() + 1e-6)

            # Smooth (لتنعيم القراءات وتجنب التذبذبات اللحظية)
            kernel = np.ones(15) / 15
            smooth = np.convolve(energies, kernel, mode="same")

            return {
                "smooth": smooth,
                "length": len(smooth),
                "duration": duration
            }
        except Exception as e:
            logger.error(f"❌ فشل تحليل الصوت في AI Director: {e}")
            return {"smooth": np.array([]), "length": 0, "duration": duration}

    def decide_events(self, video_path: str, duration: float) -> list:
        """تحديد نقاط التفاعل الذكية في لحظات السكوت (Valleys)."""
        data = self.analyze_audio(video_path, duration)
        smooth = data.get("smooth", np.array([]))
        length = data.get("length", 0)

        events = []
        used_times = []

        if length > 0:
            # البحث عن الانخفاضات (silence) التي تلي قمم (peaks)
            for i in range(10, length - 10):
                # إذا كانت الطاقة الحالية منخفضة (أقل من 0.15) وكان ما قبلها عالياً
                if smooth[i] < 0.15 and smooth[i-5] > 0.4:
                    time_sec = (i / length) * duration
                    
                    # فلترة: عدم وضع مؤثرات في أول أو آخر 5 ثواني
                    if time_sec < 5 or time_sec > duration - 5:
                        continue
                        
                    # منع التكرار القريب (مسافة أمان 8 ثواني على الأقل)
                    if any(abs(time_sec - t) < 8 for t in used_times):
                        continue
                        
                    events.append({
                        "time": round(time_sec, 2),
                        "type": "encourage"
                    })
                    used_times.append(time_sec)
                    
                    # نحدد أقصى عدد أحداث في الحلقة للحفاظ على الرقي السينمائي
                    if len(events) >= 2:
                        break

        # Fallback آمن في حال عدم اكتشاف وقفات واضحة
        if not events:
            events.append({
                "time": duration * 0.5,
                "type": "encourage"
            })

        return events
