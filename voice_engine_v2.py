"""
Advanced Voice Engine v2 - QEEMA Pipeline
Injects natural breathing, random micro-pauses, and emotional prosody.
"""
import os
import re
import random
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("qeema_voice_v2")

def text_to_advanced_ssml(text: str, emotion: str = "warm") -> str:
    """
    محرر الإيقاع البشري: يضيف توقفات عشوائية (200-400ms) وتغيير في النبرة.
    """
    # تنظيف النص
    text = re.sub(r'\s+', ' ', text).strip()
    
    # حقن توقفات ذكية عشوائية لكسر النمطية
    def add_random_pause(match):
        pause = random.randint(300, 600)
        return f'{match.group(0)} <break time="{pause}ms"/>'

    # إضافة توقفات بعد الفواصل والنقاط بشكل غير متوقع
    text = re.sub(r'[،.]', add_random_pause, text)

    # تحديد نبرة الصوت بناءً على "المود" المطلوب من المخرج
    pitch = "-2st" if emotion == "serious" else "-1st"
    rate = "85%" if emotion == "warm" else "90%"

    ssml = f"""
    <speak>
        <prosody rate="{rate}" pitch="{pitch}">
            <emphasis level="moderate">
                {text}
            </emphasis>
        </prosody>
    </speak>
    """
    return ssml.strip()

def apply_mastering_chain(input_p: Path, output_p: Path):
    """
    سلسلة الماسترينج الاحترافية: EQ دافئ + ضاغط صوت + صدى غرفة حقيقي.
    """
    filter_chain = (
        "firequalizer=gain_entry='entry(100,3);entry(250,1.5);entry(4000,-2)'," # تدفئة الترددات المنخفضة
        "aecho=0.8:0.88:20:0.05," # صدى "خفي" جداً ليعطي عمق مكاني
        "compand=attacks=0:points=-80/-80|-20/-10|-10/-5|0/-3," # ضاغط لجعل الصوت متسق القوة
        "loudnorm=I=-16:TP=-1.5" # توحيد الصوت لمعايير اليوتيوب
    )
    cmd = ["ffmpeg", "-y", "-i", str(input_p), "-af", filter_chain, str(output_p)]
    subprocess.run(cmd, capture_output=True, check=True)
