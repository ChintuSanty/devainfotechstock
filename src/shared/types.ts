export type Channel = 'me' | 'others';

export interface Meeting {
  id: string;
  title: string;
  started_at: string;
  ended_at: string | null;
  status: 'recording' | 'recorded' | 'summarising' | 'summarised' | 'failed';
  source_app: string | null;
  duration_ms: number;
  audio_path: string | null;
  segment_count?: number;
}

export interface Segment {
  id: number;
  meeting_id: string;
  channel: Channel;
  start_ms: number;
  end_ms: number;
  text: string;
  confidence: number | null;
}

export interface Artifact {
  id: number;
  meeting_id: string;
  kind: 'minutes' | 'suggestions' | 'notes';
  content: string;
  model: string | null;
}

export interface RecordingStatus {
  state: 'idle' | 'starting' | 'recording' | 'paused' | 'stopping' | 'error';
  meeting_id: string | null;
  title: string | null;
  started_at: string | null;
  elapsed_ms: number;
  segment_count: number;
  pending_utterances: number;
  error: string | null;
  channels: Record<string, { active: boolean; level: number; error: string | null; recording_audio: boolean }>;
}

export interface AppSettings {
  stt_model: string;
  stt_device: string;
  stt_compute_type: string;
  stt_language: string;
  stt_beam_size: number;
  capture_system_audio: boolean;
  capture_microphone: boolean;
  system_device_id: string;
  microphone_device_id: string;
  sample_rate: number;
  vad_silence_ms: number;
  vad_min_speech_ms: number;
  max_utterance_ms: number;
  llm_provider: string;
  llm_base_url: string;
  llm_model: string;
  llm_api_key: string;
  llm_temperature: number;
  llm_context_chars: number;
  llm_timeout_seconds: number;
  store_audio: boolean;
  retention_days: number;
  auto_summarise_on_stop: boolean;
  auto_detect_meeting_apps: boolean;
  overlay_always_on_top: boolean;
}

export interface DeviceInfo {
  id: string;
  name: string;
  kind: 'loopback' | 'input';
  channels: number;
  sample_rate: number;
  is_default: boolean;
}

export interface StorageStats {
  meetings: number;
  segments: number;
  artifacts: number;
  database_bytes: number;
  audio_bytes: number;
  total_bytes: number;
  oldest_meeting_at: string | null;
  data_dir: string;
}

export interface LlmHealth {
  available: boolean;
  models: string[];
  model?: string;
  model_installed?: boolean;
  error?: string;
}

export interface MeetingApp {
  app: string;
  process: string;
  source: string;
}

export interface SidecarEvent {
  type: string;
  payload: Record<string, unknown>;
}
