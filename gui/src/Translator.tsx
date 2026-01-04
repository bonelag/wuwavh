import { createSignal, createEffect, For, Show } from "solid-js";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

interface TranslatorConfig {
  base_url: string;
  api_key: string;
  model: string;
  system_prompt: string;
  temperature: number;
  max_tokens: number;
  top_p: number;
  top_k: number;
  stream: boolean;
  threads: number;
  batch_size: number;
  delay: number;
  last_file: string;
}

interface ProgressEvent {
  thread_id: number;
  current: number;
  total: number;
  message: string;
  append: boolean;
}

const DEFAULT_SYSTEM_PROMPT = `# ROLE: Master of Game Localization (English to Vietnamese)
# CONTEXT: Wuthering Waves (Kuro Games) - Sci-fi, Post-apocalyptic, Solaris-3.

## 1. MANDATORY TECHNICAL PROTOCOL (STRICT):
- FORMAT: Always '{ID}:::{TranslatedText}'. One ID per line. NO blank lines between IDs.
- INTEGRITY: Preserve {tags}. No new braces.
- LITERALS: Keep '\\n' as literal.
- NO CHAT: Output ONLY translated content.

## 5. FINAL EXECUTION:
Translate EVERY line. Format: ID:::Text`;

export default function Translator() {
  const [config, setConfig] = createSignal<TranslatorConfig>({
    base_url: "https://api.mistral.ai/v1",
    api_key: "",
    model: "mistral-large-latest",
    system_prompt: DEFAULT_SYSTEM_PROMPT,
    temperature: 0.2,
    max_tokens: 4096,
    top_p: 1.0,
    top_k: -1,
    stream: true,
    threads: 1,
    batch_size: 50,
    delay: 1.3,
    last_file: "",
  });

  const [models, setModels] = createSignal<string[]>([]);
  const [isRunning, setIsRunning] = createSignal(false);
  const [progress, setProgress] = createSignal<Record<number, ProgressEvent>>({});
  const [logs, setLogs] = createSignal<string[]>([]);

  createEffect(() => {
    const unlisten = listen<ProgressEvent>("progress", (event) => {
      const p = event.payload;
      setProgress((prev) => ({ ...prev, [p.thread_id]: p }));
      if (p.message) {
          // Only log significant messages or errors to avoid spam
          if (p.message.startsWith("API Error") || p.message.startsWith("Exception") || p.message === "Finished.") {
               setLogs((prev) => [...prev, `[Thread ${p.thread_id}] ${p.message}`]);
          }
      }
    });
    return () => {
      unlisten.then((f) => f());
    };
  });

  const handleFileSelect = async () => {
    const selected = await open({
      multiple: false,
      filters: [{ name: "Text", extensions: ["txt", "csv"] }],
    });
    if (selected) {
      setConfig({ ...config(), last_file: selected as string });
    }
  };

  const fetchModels = async () => {
    try {
      const res = await invoke<string[]>("fetch_models", {
        baseUrl: config().base_url,
        apiKey: config().api_key,
      });
      setModels(res);
    } catch (e) {
      alert(`Error fetching models: ${e}`);
    }
  };

  const startTranslation = async () => {
    if (!config().last_file) {
      alert("Please select a file first.");
      return;
    }
    setIsRunning(true);
    setLogs([]);
    setProgress({});
    try {
      await invoke("start_translation", {
        config: config(),
        filePath: config().last_file,
      });
      alert("Translation finished!");
    } catch (e) {
      alert(`Error: ${e}`);
    } finally {
      setIsRunning(false);
    }
  };

  const stopTranslation = async () => {
    await invoke("stop_translation");
    setIsRunning(false);
  };

  return (
    <div class="p-6 max-w-4xl mx-auto bg-gray-800 rounded-xl shadow-lg text-gray-100">
      <h2 class="text-2xl font-bold mb-6 text-blue-400">WuWa Localizer (Tauri v2)</h2>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Left Column: Settings */}
        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-400">Base URL</label>
            <input
              type="text"
              class="w-full bg-gray-700 border border-gray-600 rounded p-2 focus:border-blue-500 outline-none"
              value={config().base_url}
              onInput={(e) => setConfig({ ...config(), base_url: e.currentTarget.value })}
            />
          </div>

          <div>
            <label class="block text-sm font-medium text-gray-400">API Key</label>
            <input
              type="password"
              class="w-full bg-gray-700 border border-gray-600 rounded p-2 focus:border-blue-500 outline-none"
              value={config().api_key}
              onInput={(e) => setConfig({ ...config(), api_key: e.currentTarget.value })}
            />
          </div>

          <div class="flex gap-2 items-end">
            <div class="flex-1">
              <label class="block text-sm font-medium text-gray-400">Model</label>
              <input
                list="models-list"
                class="w-full bg-gray-700 border border-gray-600 rounded p-2 focus:border-blue-500 outline-none"
                value={config().model}
                onInput={(e) => setConfig({ ...config(), model: e.currentTarget.value })}
              />
              <datalist id="models-list">
                <For each={models()}>{(m) => <option value={m} />}</For>
              </datalist>
            </div>
            <button
              onClick={fetchModels}
              class="p-2 bg-gray-600 hover:bg-gray-500 rounded transition-colors"
              title="Fetch Models"
            >
              ↻
            </button>
          </div>

          <div class="grid grid-cols-3 gap-2">
            <div>
              <label class="block text-xs text-gray-400">Threads</label>
              <input
                type="number"
                class="w-full bg-gray-700 border border-gray-600 rounded p-2"
                value={config().threads}
                onInput={(e) => setConfig({ ...config(), threads: parseInt(e.currentTarget.value) })}
              />
            </div>
            <div>
              <label class="block text-xs text-gray-400">Batch</label>
              <input
                type="number"
                class="w-full bg-gray-700 border border-gray-600 rounded p-2"
                value={config().batch_size}
                onInput={(e) => setConfig({ ...config(), batch_size: parseInt(e.currentTarget.value) })}
              />
            </div>
            <div>
              <label class="block text-xs text-gray-400">Delay (s)</label>
              <input
                type="number"
                step="0.1"
                class="w-full bg-gray-700 border border-gray-600 rounded p-2"
                value={config().delay}
                onInput={(e) => setConfig({ ...config(), delay: parseFloat(e.currentTarget.value) })}
              />
            </div>
          </div>

          <div>
            <label class="block text-sm font-medium text-gray-400">System Prompt</label>
            <textarea
              class="w-full h-32 bg-gray-700 border border-gray-600 rounded p-2 text-xs font-mono focus:border-blue-500 outline-none"
              value={config().system_prompt}
              onInput={(e) => setConfig({ ...config(), system_prompt: e.currentTarget.value })}
            />
          </div>
        </div>

        {/* Right Column: File & Progress */}
        <div class="space-y-4 flex flex-col">
          <div>
            <label class="block text-sm font-medium text-gray-400">Input File</label>
            <div class="flex gap-2">
              <input
                type="text"
                readOnly
                class="flex-1 bg-gray-700 border border-gray-600 rounded p-2 text-gray-300"
                value={config().last_file}
              />
              <button
                onClick={handleFileSelect}
                class="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded font-semibold"
              >
                📂
              </button>
            </div>
          </div>

          <div class="flex gap-4">
            <button
              onClick={startTranslation}
              disabled={isRunning()}
              class={`flex-1 py-3 rounded font-bold text-lg transition-colors ${
                isRunning()
                  ? "bg-gray-600 cursor-not-allowed text-gray-400"
                  : "bg-green-600 hover:bg-green-700 text-white"
              }`}
            >
              {isRunning() ? "Running..." : "START"}
            </button>
            <button
              onClick={stopTranslation}
              disabled={!isRunning()}
              class={`px-6 py-3 rounded font-bold text-lg transition-colors ${
                !isRunning()
                  ? "bg-gray-600 cursor-not-allowed text-gray-400"
                  : "bg-red-600 hover:bg-red-700 text-white"
              }`}
            >
              STOP
            </button>
          </div>

          <div class="flex-1 bg-gray-900 rounded border border-gray-700 p-4 overflow-y-auto max-h-[400px]">
            <h3 class="text-sm font-bold text-gray-400 mb-2">Progress</h3>
            <div class="space-y-2">
              <For each={Object.values(progress())}>
                {(p) => (
                  <div class="bg-gray-800 p-2 rounded text-xs">
                    <div class="flex justify-between mb-1">
                      <span class="font-bold text-blue-400">Thread {p.thread_id}</span>
                      <span class="text-gray-400">
                        {p.current} / {p.total} ({Math.round((p.current / p.total) * 100)}%)
                      </span>
                    </div>
                    <div class="w-full bg-gray-700 rounded-full h-2">
                      <div
                        class="bg-blue-500 h-2 rounded-full transition-all duration-300"
                        style={{ width: `${(p.current / p.total) * 100}%` }}
                      />
                    </div>
                    <div class="mt-1 text-gray-500 truncate">{p.message}</div>
                  </div>
                )}
              </For>
            </div>
            
            <h3 class="text-sm font-bold text-gray-400 mt-4 mb-2">Logs</h3>
            <div class="text-xs font-mono text-gray-500 space-y-1">
                <For each={logs()}>{(log) => <div>{log}</div>}</For>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
