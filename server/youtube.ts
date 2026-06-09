/**
 * QEEMA — YouTube Upload (حقيقي)
 * رفع resumable عبر OAuth refresh token. لو الـ credentials ناقصة -> Error واضح
 * (مفيش "محاكاة نجاح" زي القديم).
 */
import { google } from 'googleapis';
import * as fs from 'fs';
import { YOUTUBE } from './config.ts';

export interface UploadParams {
  filePath: string;
  title: string;
  description: string;
  tags: string[];
}

export async function uploadVideo(params: UploadParams): Promise<string> {
  if (!fs.existsSync(params.filePath)) {
    throw new Error(`[youtube] ملف الفيديو غير موجود: ${params.filePath}`);
  }
  const oauth2 = new google.auth.OAuth2(YOUTUBE.clientId(), YOUTUBE.clientSecret());
  oauth2.setCredentials({ refresh_token: YOUTUBE.refreshToken() });

  const youtube = google.youtube({ version: 'v3', auth: oauth2 });
  console.log(`[youtube] بدء رفع: "${params.title}" (${YOUTUBE.privacyStatus})`);

  const res = await youtube.videos.insert({
    part: ['snippet', 'status'],
    requestBody: {
      snippet: {
        title: params.title.slice(0, 100),
        description: params.description.slice(0, 4900),
        tags: params.tags?.slice(0, 30),
        categoryId: '27', // Education
        defaultLanguage: 'ar',
        defaultAudioLanguage: 'ar',
      },
      status: {
        privacyStatus: YOUTUBE.privacyStatus,
        selfDeclaredMadeForKids: true, // محتوى للأطفال
      },
    },
    media: { body: fs.createReadStream(params.filePath) },
  });

  const videoId = res.data.id;
  if (!videoId) throw new Error('[youtube] الرد مفهوش Video ID.');
  console.log(`[youtube] تم الرفع: https://youtube.com/watch?v=${videoId}`);
  return videoId;
}
