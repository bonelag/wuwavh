use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter};
use std::time::Duration;
use std::io::Write;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct TranslatorConfig {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub system_prompt: String,
    pub temperature: f64,
    pub max_tokens: u32,
    pub top_p: f64,
    pub top_k: i32,
    pub stream: bool,
    pub threads: usize,
    pub batch_size: usize,
    pub delay: f64,
    pub last_file: String,
}

#[derive(Clone, Serialize)]
struct ProgressEvent {
    thread_id: usize,
    current: usize,
    total: usize,
    message: String,
    append: bool,
}

pub struct TranslatorState {
    pub stop_flag: Arc<Mutex<bool>>,
}

impl TranslatorState {
    pub fn new() -> Self {
        Self {
            stop_flag: Arc::new(Mutex::new(false)),
        }
    }
}

#[tauri::command]
pub async fn stop_translation(state: tauri::State<'_, TranslatorState>) -> Result<(), String> {
    let mut stop = state.stop_flag.lock().map_err(|e| e.to_string())?;
    *stop = true;
    Ok(())
}

#[tauri::command]
pub async fn fetch_models(base_url: String, api_key: String) -> Result<Vec<String>, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/models", base_url.trim_end_matches('/'));
    
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", api_key))
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.status().is_success() {
        return Err(format!("Failed to fetch models: {}", resp.status()));
    }

    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    
    let mut models = Vec::new();
    if let Some(data) = json.get("data") {
        if let Some(arr) = data.as_array() {
            for item in arr {
                if let Some(id) = item.get("id").and_then(|v| v.as_str()) {
                    models.push(id.to_string());
                }
            }
        }
    } else if let Some(arr) = json.as_array() {
        // Some APIs return direct array
        for item in arr {
             if let Some(id) = item.get("id").and_then(|v| v.as_str()) {
                models.push(id.to_string());
            }
        }
    }
    
    models.sort();
    Ok(models)
}

#[tauri::command]
pub async fn start_translation(
    app: AppHandle,
    state: tauri::State<'_, TranslatorState>,
    config: TranslatorConfig,
    file_path: String,
) -> Result<(), String> {
    // Reset stop flag
    {
        let mut stop = state.stop_flag.lock().map_err(|e| e.to_string())?;
        *stop = false;
    }

    let content = std::fs::read_to_string(&file_path).map_err(|e| e.to_string())?;
    let lines: Vec<String> = content.lines().map(|s| s.to_string()).collect();
    
    // Parse lines to find ID:::Text or just Text
    // We need to keep track of original indices to reconstruct the file
    let mut work_items = Vec::new();
    let output_lines = lines.clone();
    
    // Check for header (0:::)
    let start_idx = if !lines.is_empty() && lines[0].starts_with("0:::") { 1 } else { 0 };
    
    for (i, line) in lines.iter().enumerate().skip(start_idx) {
        work_items.push((i, line.clone()));
    }

    let total_items = work_items.len();
    let num_threads = config.threads.max(1);
    let chunk_size = (total_items as f64 / num_threads as f64).ceil() as usize;
    
    let chunks: Vec<Vec<(usize, String)>> = work_items.chunks(chunk_size).map(|c| c.to_vec()).collect();
    
    let stop_flag = state.stop_flag.clone();
    let config = Arc::new(config);
    let output_mutex = Arc::new(Mutex::new(output_lines));
    
    // Create a semaphore to limit concurrent requests if needed, 
    // but here we use threads as the limit.
    // Actually, we will spawn tasks.
    
    let mut handles = vec![];

    for (thread_id, chunk) in chunks.into_iter().enumerate() {
        let config = config.clone();
        let stop_flag = stop_flag.clone();
        let app_handle = app.clone();
        let output_mutex = output_mutex.clone();
        
        let handle = tokio::spawn(async move {
            let thread_id = thread_id + 1;
            let total_in_chunk = chunk.len();
            let start_idx = chunk.first().map(|x| x.0).unwrap_or(0);
            let end_idx = chunk.last().map(|x| x.0).unwrap_or(0);
            
            let _ = app_handle.emit("progress", ProgressEvent {
                thread_id,
                current: 0,
                total: total_in_chunk,
                message: format!("Ready. Range: {}-{}", start_idx, end_idx),
                append: false,
            });

            let client = reqwest::Client::new();
            let mut processed = 0;

            for batch in chunk.chunks(config.batch_size) {
                if *stop_flag.lock().unwrap() {
                    let _ = app_handle.emit("progress", ProgressEvent {
                        thread_id,
                        current: processed,
                        total: total_in_chunk,
                        message: "Stopped.".to_string(),
                        append: false,
                    });
                    break;
                }

                // Prepare batch
                let batch_lines: Vec<String> = batch.iter().map(|(_, s)| s.clone()).collect();
                let batch_indices: Vec<usize> = batch.iter().map(|(i, _)| *i).collect();
                
                // Call API
                let translated = call_api_translate(
                    &client, 
                    &config, 
                    &batch_lines, 
                    &app_handle, 
                    thread_id,
                    processed,
                    total_in_chunk
                ).await;

                // Update output
                {
                    let mut out = output_mutex.lock().unwrap();
                    for (idx, text) in batch_indices.iter().zip(translated.iter()) {
                        out[*idx] = text.clone();
                    }
                    // Save temp progress (optional, maybe too heavy to do every batch if many threads)
                    // For now, let's skip saving to file every batch to avoid lock contention, 
                    // or do it less frequently.
                }
                
                processed += batch.len();
                let _ = app_handle.emit("progress", ProgressEvent {
                    thread_id,
                    current: processed,
                    total: total_in_chunk,
                    message: "".to_string(), // Clear message or keep last
                    append: false,
                });
                
                // Delay
                tokio::time::sleep(Duration::from_secs_f64(config.delay)).await;
            }
            
            let _ = app_handle.emit("progress", ProgressEvent {
                thread_id,
                current: total_in_chunk,
                total: total_in_chunk,
                message: "Finished.".to_string(),
                append: false,
            });
        });
        handles.push(handle);
    }

    // Wait for all threads
    for h in handles {
        let _ = h.await;
    }

    // Save final result
    if !*stop_flag.lock().unwrap() {
        let final_lines = output_mutex.lock().unwrap();
        let output_path = "tran.txt"; // Or derive from input path
        let mut file = std::fs::File::create(output_path).map_err(|e| e.to_string())?;
        for line in final_lines.iter() {
            writeln!(file, "{}", line).map_err(|e| e.to_string())?;
        }
    }

    Ok(())
}

async fn call_api_translate(
    client: &reqwest::Client,
    config: &TranslatorConfig,
    lines: &[String],
    app: &AppHandle,
    thread_id: usize,
    current_processed: usize,
    total_in_chunk: usize,
) -> Vec<String> {
    let prompt = lines.join("\n") + "\n\nREMINDER: Format 'ID:::TranslatedText'.";
    
    let url = format!("{}/chat/completions", config.base_url.trim_end_matches('/'));
    
    let mut payload = serde_json::json!({
        "model": config.model,
        "messages": [
            {"role": "system", "content": config.system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "top_p": config.top_p,
        "stream": config.stream
    });

    if config.top_k > 0 {
        payload["top_k"] = serde_json::json!(config.top_k);
    }

    let mut result_lines = lines.to_vec(); // Default to original on failure

    let resp_res = client.post(&url)
        .header("Authorization", format!("Bearer {}", config.api_key))
        .json(&payload)
        .send()
        .await;

    match resp_res {
        Ok(resp) => {
            if !resp.status().is_success() {
                let _ = app.emit("progress", ProgressEvent {
                    thread_id,
                    current: current_processed,
                    total: total_in_chunk,
                    message: format!("API Error: {}", resp.status()),
                    append: true,
                });
                return result_lines;
            }

            let mut full_content = String::new();

            if config.stream {
                use futures_util::StreamExt;
                let mut stream = resp.bytes_stream();
                
                while let Some(item) = stream.next().await {
                    if let Ok(chunk) = item {
                        let s = String::from_utf8_lossy(&chunk);
                        for line in s.lines() {
                            let line = line.trim();
                            if line.starts_with("data: ") {
                                let data = &line[6..];
                                if data == "[DONE]" { break; }
                                if let Ok(json) = serde_json::from_str::<serde_json::Value>(data) {
                                    if let Some(content) = json["choices"][0]["delta"]["content"].as_str() {
                                        full_content.push_str(content);
                                        // Emit log update (optional, might be too spammy)
                                        // let _ = app.emit("progress", ProgressEvent {
                                        //     thread_id,
                                        //     current: current_processed,
                                        //     total: total_in_chunk,
                                        //     message: content.to_string(),
                                        //     append: true,
                                        // });
                                    }
                                }
                            }
                        }
                    }
                }
            } else {
                if let Ok(json) = resp.json::<serde_json::Value>().await {
                    if let Some(content) = json["choices"][0]["message"]["content"].as_str() {
                        full_content = content.to_string();
                    }
                }
            }

            // Parse results
            let translated_lines: Vec<&str> = full_content.trim().split('\n').collect();
            let mut translated_map = std::collections::HashMap::new();
            
            for line in translated_lines {
                if let Some((id, text)) = line.split_once(":::") {
                    translated_map.insert(id.trim().to_string(), text.trim().to_string());
                } else {
                    // Try regex fallback if needed, or simple heuristic
                    // For now, simple split
                }
            }

            let mut new_results = Vec::new();
            for line in lines {
                if let Some((id, _)) = line.split_once(":::") {
                    let id = id.trim();
                    if let Some(trans) = translated_map.get(id) {
                        new_results.push(format!("{}:::{}", id, trans));
                    } else {
                        new_results.push(line.clone());
                    }
                } else {
                    new_results.push(line.clone());
                }
            }
            result_lines = new_results;
        }
        Err(e) => {
            let _ = app.emit("progress", ProgressEvent {
                thread_id,
                current: current_processed,
                total: total_in_chunk,
                message: format!("Exception: {}", e),
                append: true,
            });
        }
    }

    result_lines
}
