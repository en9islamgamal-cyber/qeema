/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { GoogleGenAI, Type } from '@google/genai';
import { DB } from './db.ts';

// Helper for sleeping during backoffs
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export class GeminiService {
  /**
   * Selects the first available Gemini API key from the pool, initializing and validating it.
   */
  private static async getActiveClient(): Promise<{ client: GoogleGenAI; keyName: string }> {
    const keys = [
      { envVar: 'GEMINI_API_KEY', name: 'KeyA' },
      { envVar: 'GEMINI_API_KEY_2', name: 'KeyB' },
      { envVar: 'GEMINI_API_KEY_3', name: 'KeyC' },
    ];

    for (const keyDef of keys) {
      const keyValue = process.env[keyDef.envVar];
      if (!keyValue) {
        continue; // Key not defined in environment variables
      }

      const metric = await DB.getKeyMetric(keyDef.name);
      if (metric.status === 'active') {
        // Return this client
        const client = new GoogleGenAI({
          apiKey: keyValue,
          httpOptions: {
            headers: {
              'User-Agent': 'aistudio-build',
            },
          },
        });
        return { client, keyName: keyDef.name };
      }
    }

    // Fallback: If all are marked exhausted/rate_limited but we have keys, resurrect them all and try key A first
    console.warn('[GEMINI-SERVICE] All keys in pool currently marked exhausted or rate-limited. Auto-resetting rotation pools for safety.');
    for (const keyDef of keys) {
      if (process.env[keyDef.envVar]) {
        await DB.makeKeyActive(keyDef.name);
      }
    }

    // Try primary again after resurrection
    if (process.env.GEMINI_API_KEY) {
      const client = new GoogleGenAI({
        apiKey: process.env.GEMINI_API_KEY,
        httpOptions: { headers: { 'User-Agent': 'aistudio-build' } },
      });
      return { client, keyName: 'KeyA' };
    }

    throw new Error('No valid GEMINI_API_KEY environment variables are present in the pipeline execution workspace.');
  }

  /**
   * Generates text content with safety, retry with backoff, and multi-key fallback support.
   */
  static async generateText(
    prompt: string,
    systemInstruction?: string,
    episodeId: string | null = null,
    retriesRemaining = 3,
    backoffMs = 2000
  ): Promise<string> {
    try {
      const { client, keyName } = await this.getActiveClient();
      await DB.trackKeyUsage(keyName);

      const response = await client.models.generateContent({
        model: 'gemini-3.5-flash',
        contents: prompt,
        config: systemInstruction ? { systemInstruction } : undefined,
      });

      const text = response.text || '';
      if (!text) {
        throw new Error('Gemini API returned an empty or undefined string output text response.');
      }
      return text.trim();
    } catch (error: any) {
      const errorStr = String(error?.message || error);
      const isQuotaError = errorStr.includes('429') || errorStr.includes('Quota') || errorStr.includes('limit');
      
      console.error(`[GEMINI][ERROR] Failed generation with active key. Attempts left: ${retriesRemaining}. Quota limit: ${isQuotaError}`, error);

      if (retriesRemaining > 0) {
        if (isQuotaError) {
          // Identify current active key, mark it rate_limited / exhausted, and immediately fallback
          const { keyName } = await this.getActiveClient();
          await DB.markKeyExhausted(keyName);
          await DB.log(episodeId, 'generation', 'warn', `Rotated API key away from ${keyName} due to quota limit detection.`);
          return this.generateText(prompt, systemInstruction, episodeId, retriesRemaining - 1, backoffMs);
        } else {
          // Non-quota temporary network glitch. Backoff and retry
          await sleep(backoffMs);
          return this.generateText(prompt, systemInstruction, episodeId, retriesRemaining - 1, backoffMs * 1.5);
        }
      }
      throw new Error(`Gemini rotation pool exhausted or critically failed execution: ${errorStr}`);
    }
  }

  /**
   * Generates structured JSON responses using standard AI schemas and failsafe structured configurations.
   */
  static async generateJSON<T>(
    prompt: string,
    schemaStructure: any,
    systemInstruction?: string,
    episodeId: string | null = null,
    retriesRemaining = 3,
    backoffMs = 2000
  ): Promise<T> {
    try {
      const { client, keyName } = await this.getActiveClient();
      await DB.trackKeyUsage(keyName);

      const response = await client.models.generateContent({
        model: 'gemini-3.5-flash',
        contents: prompt,
        config: {
          systemInstruction,
          responseMimeType: 'application/json',
          responseSchema: schemaStructure,
        },
      });

      const text = response.text || '';
      if (!text) {
        throw new Error('Gemini responded with empty content body for JSON output payload.');
      }

      return JSON.parse(text) as T;
    } catch (error: any) {
      const errorStr = String(error?.message || error);
      const isQuotaError = errorStr.includes('429') || errorStr.includes('Quota') || errorStr.includes('limit');

      console.error(`[GEMINI][JSON_ERROR] Attempts remaining: ${retriesRemaining}. Quota limit: ${isQuotaError}`, error);

      if (retriesRemaining > 0) {
        if (isQuotaError) {
          const { keyName } = await this.getActiveClient();
          await DB.markKeyExhausted(keyName);
          await DB.log(episodeId, 'generation', 'warn', `Rotated API JSON key away from ${keyName} due to quota-exhausted state.`);
          return this.generateJSON<T>(prompt, schemaStructure, systemInstruction, episodeId, retriesRemaining - 1, backoffMs);
        } else {
          await sleep(backoffMs);
          return this.generateJSON<T>(prompt, schemaStructure, systemInstruction, episodeId, retriesRemaining - 1, backoffMs * 1.5);
        }
      }
      throw new Error(`Gemini JSON generator critically timed out or failed: ${errorStr}`);
    }
  }
}
export { Type };
