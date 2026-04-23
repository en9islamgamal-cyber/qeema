    def apply_to_episode(self, video_path: str, script: EpisodeScript, output_path: str) -> str:
        """
        يطبق جميع تأثيرات التلعيب (اللوجو + شريط التقدم + التشجيع) 
        في مسار واحد (Single-Pass Complex Filter) لضمان أعلى جودة.
        """
        duration = _probe_duration(video_path)
        if duration <= 0:
            logger.error("❌ لم أتمكن من قراءة مدة الفيديو، سيتم تجاوز التلعيب.")
            shutil.copy(video_path, output_path)
            return output_path

        logger.info(f"🎮 بدء إضافة اللوجو وتأثيرات التلعيب معاً في مسار واحد...")

        logo_path = str(Paths.LOGO_PRIMARY) # جلب مسار اللوجو من config
        has_logo = Path(logo_path).exists()
        
        # تجهيز المدخلات
        inputs = ["-y", "-i", video_path]
        if has_logo:
            inputs.extend(["-i", logo_path]) # إدخال اللوجو كملف ثاني

        filter_chains = []

        # 1. طبقة اللوجو (إذا كان موجوداً)
        if has_logo:
            # تصغير اللوجو وشفافية 85%، ثم وضعه أعلى اليمين
            filter_chains.append("[1:v]scale=160:-1,format=rgba,colorchannelmixer=aa=0.85[wm];[0:v][wm]overlay=W-w-30:30[v_base]")
        else:
            filter_chains.append("[0:v]copy[v_base]") # تمرير الفيديو كما هو إذا لم يوجد لوجو

        # 2. طبقة شريط التقدم الديناميكي
        filter_chains.append("[v_base]drawbox=x=0:y=H-12:w=W:h=12:color=black@0.6:t=fill[v_box1]")
        filter_chains.append(f"[v_box1]drawbox=x=0:y=H-12:w=W*(t/{duration}):h=12:color=#FFD700@0.9:t=fill[v_box2]")

        # 3. طبقة النص التشجيعي العشوائي
        if self.font:
            encouragement = random.choice(ENCOURAGEMENTS)
            safe_text = self._prepare_arabic_text(encouragement)
            
            t_start = duration * 0.5
            t_end = t_start + 4.0
            alpha_logic = f"if(lt(t,{t_start+0.5}),(t-{t_start})/0.5,if(gt(t,{t_end-0.5}),({t_end}-t)/0.5,1))"
            
            text_filter = (
                f"[v_box2]drawtext=fontfile='{self.font}':text='{safe_text}':"
                f"fontcolor=yellow:fontsize=75:x=(W-text_w)/2:y=H*0.15:"
                f"enable='between(t,{t_start},{t_end})':alpha='{alpha_logic}':"
                f"shadowcolor=black@0.8:shadowx=4:shadowy=4[vout]"
            )
            filter_chains.append(text_filter)
        else:
            filter_chains.append("[v_box2]copy[vout]")

        # تجميع كل الفلاتر بفاصل منقوطة (Semicolon) لتشغيلها بالترتيب
        vf_string = ";".join(filter_chains)

        # أمر الرندرة المعماري الجديد
        cmd = ["ffmpeg"] + inputs + [
            "-filter_complex", vf_string,
            "-map", "[vout]", # أخذ الصورة النهائية
            "-map", "0:a",    # أخذ الصوت من الفيديو الأصلي
            "-c:v", VideoConfig.CODEC, 
            "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF), 
            "-preset", "fast",
            "-pix_fmt", VideoConfig.PIX_FMT, 
            "-c:a", "copy", # الصوت لا يُعاد ترميزه
            output_path
        ]

        if _run(cmd, timeout=900):
            logger.info("✅ تمت إضافة اللوجو وتأثيرات التلعيب بنجاح.")
            return output_path
        else:
            logger.warning("⚠️ فشل التلعيب، سيتم استخدام الفيديو الأصلي كإجراء احتياطي.")
            shutil.copy(video_path, output_path)
            return output_path
