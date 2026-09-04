import { useEffect, useState } from 'react';
import { api } from '../../shared/api';
import { bridge } from '../../shared/bridge';
import type { AppSettings, DeviceInfo, LlmHealth } from '../../shared/types';

const STT_MODELS = [
  { value: 'tiny.en', label: 'tiny.en - fastest, roughest (very old machines)' },
  { value: 'base.en', label: 'base.en - fast, usable' },
  { value: 'small.en', label: 'small.en - recommended balance' },
  { value: 'medium.en', label: 'medium.en - most accurate, needs a fast CPU or GPU' },
  { value: 'large-v3', label: 'large-v3 - best quality, GPU strongly advised' },
];

export function SettingsView() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [devices, setDevices] = useState<{ loopback: DeviceInfo[]; input: DeviceInfo[] }>({ loopback: [], input: [] });
  const [llm, setLlm] = useState<LlmHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void api.getSettings().then(setSettings).catch((cause) => setError((cause as Error).message));
    void api.devices().then(setDevices).catch(() => undefined);
    void api.llmHealth().then(setLlm).catch(() => undefined);
  }, []);

  const update = async (values: Partial<AppSettings>) => {
    try {
      setSettings(await api.saveSettings(values));
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1600);
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  if (!settings) {
    return <div className="empty">{error ?? 'Loading settings...'}</div>;
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p>Nothing here sends data anywhere. All defaults point at this machine.</p>
        </div>
        {saved && <span className="badge ok">Saved</span>}
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <h2>Audio capture</h2>
        <p className="hint">
          Meeting audio is taken from the system output loopback, so it works with every meeting app at once - Zoom,
          Teams, Meet in a browser, Webex, Slack huddles. Your microphone is captured separately so you can be told
          apart from everyone else.
        </p>
        <div className="stack">
          <label className="switch">
            <input
              type="checkbox"
              checked={settings.capture_system_audio}
              onChange={(event) => update({ capture_system_audio: event.target.checked })}
            />
            Capture system audio (everyone else)
          </label>
          <label className="switch">
            <input
              type="checkbox"
              checked={settings.capture_microphone}
              onChange={(event) => update({ capture_microphone: event.target.checked })}
            />
            Capture microphone (you)
          </label>

          <div className="grid-2">
            <div className="field">
              <label>System audio device</label>
              <select
                value={settings.system_device_id}
                onChange={(event) => update({ system_device_id: event.target.value })}
              >
                <option value="">Default speakers (loopback)</option>
                {devices.loopback.map((device) => (
                  <option key={device.id} value={device.id}>
                    {device.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Microphone</label>
              <select
                value={settings.microphone_device_id}
                onChange={(event) => update({ microphone_device_id: event.target.value })}
              >
                <option value="">Default microphone</option>
                {devices.input.map((device) => (
                  <option key={device.id} value={device.id}>
                    {device.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Speech recognition</h2>
        <p className="hint">
          Runs locally with faster-whisper. Larger models are more accurate and slower; changing the model reloads it on
          the next recording.
        </p>
        <div className="grid-2">
          <div className="field">
            <label>Model</label>
            <select value={settings.stt_model} onChange={(event) => update({ stt_model: event.target.value })}>
              {STT_MODELS.map((model) => (
                <option key={model.value} value={model.value}>
                  {model.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Compute device</label>
            <select value={settings.stt_device} onChange={(event) => update({ stt_device: event.target.value })}>
              <option value="auto">Auto (GPU if available)</option>
              <option value="cpu">CPU (int8)</option>
              <option value="cuda">NVIDIA GPU (float16)</option>
            </select>
          </div>
          <div className="field">
            <label>Silence before a line is closed (ms)</label>
            <input
              type="number"
              min={200}
              max={3000}
              step={50}
              value={settings.vad_silence_ms}
              onChange={(event) => update({ vad_silence_ms: Number(event.target.value) })}
            />
            <p className="note">Lower is more responsive; higher avoids chopping sentences in half.</p>
          </div>
          <div className="field">
            <label>Language</label>
            <input
              value={settings.stt_language}
              onChange={(event) => update({ stt_language: event.target.value })}
              placeholder="en"
            />
            <p className="note">Leave blank to auto-detect. The .en models are English only.</p>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Local language model</h2>
        <p className="hint">
          Used to write the minutes and suggestions. Ollama on this machine by default - nothing is sent over the
          internet.
        </p>
        {llm && (
          <div className={llm.available ? 'info-banner' : 'error-banner'}>
            {llm.available
              ? llm.model_installed
                ? `Connected. ${llm.model} is installed.`
                : `Connected, but ${llm.model} is not pulled yet. Run: ollama pull ${llm.model}`
              : `Cannot reach the model server. Install Ollama and run: ollama serve`}
          </div>
        )}
        <div className="grid-2">
          <div className="field">
            <label>Provider</label>
            <select value={settings.llm_provider} onChange={(event) => update({ llm_provider: event.target.value })}>
              <option value="ollama">Ollama</option>
              <option value="openai_compatible">OpenAI-compatible (llama.cpp, LM Studio, vLLM)</option>
            </select>
          </div>
          <div className="field">
            <label>Server address</label>
            <input value={settings.llm_base_url} onChange={(event) => update({ llm_base_url: event.target.value })} />
          </div>
          <div className="field">
            <label>Model</label>
            <input value={settings.llm_model} onChange={(event) => update({ llm_model: event.target.value })} />
            {llm?.models?.length ? <p className="note">Installed: {llm.models.slice(0, 6).join(', ')}</p> : null}
          </div>
          <div className="field">
            <label>Transcript chunk size (characters)</label>
            <input
              type="number"
              min={2000}
              max={60000}
              step={1000}
              value={settings.llm_context_chars}
              onChange={(event) => update({ llm_context_chars: Number(event.target.value) })}
            />
            <p className="note">Long meetings are summarised in chunks. Keep this inside your model's context window.</p>
          </div>
        </div>
        <label className="switch" style={{ marginTop: 12 }}>
          <input
            type="checkbox"
            checked={settings.auto_summarise_on_stop}
            onChange={(event) => update({ auto_summarise_on_stop: event.target.checked })}
          />
          Write the minutes automatically when a recording stops
        </label>
      </div>

      <div className="card">
        <h2>Behaviour</h2>
        <div className="stack">
          <label className="switch">
            <input
              type="checkbox"
              checked={settings.auto_detect_meeting_apps}
              onChange={(event) => update({ auto_detect_meeting_apps: event.target.checked })}
            />
            Detect running meeting apps and label recordings with them
          </label>
          <div className="row">
            <button onClick={() => void api.resetSettings().then(setSettings)}>Reset all settings</button>
            <button
              onClick={async () => {
                await bridge()?.restartSidecar();
                window.location.reload();
              }}
            >
              Restart the local service
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
