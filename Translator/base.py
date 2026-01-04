import os
import shutil
import json
import re
import time
import threading
import math
import requests
import queue
import customtkinter as ctk
from tkinter import filedialog, messagebox
import ctypes

# Enable High DPI
# try:
#     ctypes.windll.shcore.SetProcessDpiAwareness(1)
# except Exception:
#     pass

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# --- CONSTANTS & CONFIG ---
CONFIG_FILE = "config.json"
INPUT_FILE = "temp.txt"
OUTPUT_FILE = "tran.txt"
TEMP_OUTPUT_FILE = "temp_translating.txt"

DEFAULT_SYSTEM_PROMPT = (
    "# ROLE: Master of Game Localization (English to Vietnamese)\n"
    "# CONTEXT: Wuthering Waves (Kuro Games) - Sci-fi, Post-apocalyptic, Solaris-3.\n\n"
    "## 1. MANDATORY TECHNICAL PROTOCOL (STRICT):\n"
    "- FORMAT: Always '{ID}:::{TranslatedText}'. One ID per line. NO blank lines between IDs.\n"
    "- INTEGRITY: Preserve {tags}. No new braces.\n"
    "- LITERALS: Keep '\\n' as literal.\n"
    "- NO CHAT: Output ONLY translated content.\n\n"
    "## 5. FINAL EXECUTION:\n"
    "Translate EVERY line. Format: ID:::Text"
)

DEFAULT_CONFIG = {
    "base_url": "https://api.mistral.ai/v1",
    "api_key": "",
    "model": "mistral-large-latest",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "temperature": 0.2,
    "max_tokens": 4096,
    "top_p": 1.0,
    "top_k": -1, # -1 means ignore
    "stream": True,
    "threads": 1,
    "batch_size": 50,
    "delay": 1.3,
    "last_file": "temp.txt"
}

# --- GLOBAL STATE ---
config_data = DEFAULT_CONFIG.copy()
request_lock = threading.Lock()
file_write_lock = threading.Lock()
last_request_time = 0
shared_output_lines = [] 
stop_event = threading.Event()
fetched_models_cache = []

# --- UTILS ---
def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                # Update default with loaded to ensure all keys exist
                for k, v in loaded.items():
                    config_data[k] = v
        except Exception as e:
            print(f"Error loading config: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving config: {e}")

def wait_for_slot(delay_sec):
    global last_request_time
    with request_lock:
        current_time = time.time()
        elapsed = current_time - last_request_time
        if elapsed < delay_sec:
            time.sleep(delay_sec - elapsed)
        last_request_time = time.time()

def save_progress_file():
    with file_write_lock:
        with open(TEMP_OUTPUT_FILE, 'w', encoding='utf-8') as f:
            for line in shared_output_lines:
                f.write(line + '\n')

# --- API LOGIC ---
def call_api_translate(batch_lines, settings, log_callback=None):
    if stop_event.is_set(): return batch_lines

    prompt = "\n".join(batch_lines) + "\n\nREMINDER: Format 'ID:::TranslatedText'."
    
    wait_for_slot(settings['delay'])
    
    endpoint = f"{settings['base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings['api_key']}"
    }
    
    payload = {
        "model": settings['model'],
        "messages": [
            {"role": "system", "content": settings['system_prompt']},
            {"role": "user", "content": prompt},
        ],
        "temperature": settings['temperature'],
        "max_tokens": settings['max_tokens'],
        "top_p": settings['top_p'],
        "stream": settings['stream']
    }
    
    # Optional parameters
    if settings.get('top_k', -1) > 0:
        payload['top_k'] = settings['top_k']

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=120, stream=settings['stream'])
        
        if response.status_code != 200:
            err = f"API Error {response.status_code}: {response.text}"
            if log_callback: log_callback(err)
            return batch_lines

        full_content = ""
        
        if settings['stream']:
            for line in response.iter_lines():
                if stop_event.is_set(): break
                if line:
                    decoded = line.decode('utf-8').strip()
                    if decoded.startswith("data: "):
                        data_str = decoded[6:]
                        if data_str == "[DONE]": break
                        try:
                            data_json = json.loads(data_str)
                            delta = data_json['choices'][0].get('delta', {})
                            content = delta.get('content', '')
                            if content:
                                full_content += content
                                if log_callback: log_callback(content, end="")
                        except:
                            pass
            if log_callback: log_callback("\n[Stream Finished]")
        else:
            # Non-stream
            json_resp = response.json()
            full_content = json_resp['choices'][0]['message']['content']
            if log_callback: log_callback(f"Received: {len(full_content)} chars")

        translated_lines = full_content.strip().split('\n')
        translated_map = {}
        for line in translated_lines:
            if ':::' in line:
                parts = line.split(':::', 1)
                t_id = parts[0].strip()
                t_text = parts[1].strip()
                translated_map[t_id] = t_text
            else:
                match = re.match(r"^(\d+)\s*(?:[|:>.)\]])\s*(.*)", line)
                if match:
                    translated_map[match.group(1)] = match.group(2).strip()

        results = []
        for line in batch_lines:
            o_id = line.split(':::')[0].strip()
            final_text = translated_map.get(o_id, line.split(':::')[1].strip() if ':::' in line else line)
            results.append(f"{o_id}:::{final_text}")
        return results

    except Exception as e:
        if log_callback: log_callback(f"Exception: {e}")
        return batch_lines

def worker_process(thread_id, chunk_data, settings, ui_callback):
    """
    ui_callback: function(current, total, log_msg)
    """
    local_lines = [item[0] for item in chunk_data]
    local_indices = [item[1] for item in chunk_data]
    total_items = len(local_lines)
    
    start_idx = local_indices[0] if local_indices else 0
    end_idx = local_indices[-1] if local_indices else 0
    
    # Init UI
    ui_callback(0, total_items, f"Ready. Range: {start_idx}-{end_idx}")

    batch_size = settings['batch_size']
    processed = 0

    for i in range(0, total_items, batch_size):
        if stop_event.is_set():
            ui_callback(processed, total_items, "Stopped.")
            break

        batch_lines = local_lines[i:i+batch_size]
        batch_indices = local_indices[i:i+batch_size]
        
        # Define log callback for this thread
        def thread_log(msg, end="\n"):
            ui_callback(processed, total_items, msg, append=True)

        translated_batch = call_api_translate(batch_lines, settings, log_callback=thread_log)
        
        for idx, result_line in zip(batch_indices, translated_batch):
            shared_output_lines[idx] = result_line
        
        save_progress_file()
        processed += len(batch_lines)
        ui_callback(processed, total_items, "", append=False)
    
    ui_callback(total_items, total_items, "Finished.")

# --- CUSTOM WIDGETS ---

class SearchableComboBox(ctk.CTkFrame):
    """
    A searchable combobox that uses an internal Frame overlay instead of Toplevel,
    so the dropdown stays attached to the main window.
    """
    def __init__(self, master, variable=None, values=None, width=200, height=32, load_command=None, **kwargs):
        super().__init__(master, width=width, height=height, fg_color="transparent", **kwargs)
        self.variable = variable
        self.values = values or []
        self.load_command = load_command
        self.root_window = self.winfo_toplevel() # Get the root CTk window
        self.grid_columnconfigure(0, weight=1)
        
        self.entry = ctk.CTkEntry(self, width=width-30, height=height)
        self.entry.grid(row=0, column=0, sticky="ew")
        if self.variable: self.entry.configure(textvariable=self.variable)
            
        self.btn_arrow = ctk.CTkButton(self, text="▼", width=30, height=height, command=self.on_arrow_click)
        self.btn_arrow.grid(row=0, column=1, sticky="w", padx=(2, 0))
        
        self.dropdown_frame = None # The overlay frame
        self.search_var = None

    def set_values(self, values):
        self.values = values
        if self.dropdown_frame and self.dropdown_frame.winfo_exists():
            self.populate_list(self.values)

    def on_arrow_click(self):
        if not self.values and self.load_command:
            self.load_command()
        self.toggle_dropdown()

    def toggle_dropdown(self):
        if self.dropdown_frame and self.dropdown_frame.winfo_exists():
            self.dropdown_frame.destroy()
            self.dropdown_frame = None
            return
        
        # Create dropdown as a Frame placed on the ROOT WINDOW
        self.dropdown_frame = ctk.CTkFrame(self.root_window, corner_radius=8, border_width=1, border_color="#555")
        
        # Calculate position relative to root window using absolute coords
        self.update_idletasks()
        
        # Get scaling factor
        try:
            scaling = self.root_window._get_window_scaling()
        except:
            scaling = 1.0
        
        # Get title bar height offset (difference between outer and inner window position)
        # This accounts for window decorations
        outer_y = self.root_window.winfo_rooty()
        inner_y = self.root_window.winfo_y() # This is Y position on screen
        
        # Get widget position - use ENTRY for x and width, not the whole frame
        entry_x = self.entry.winfo_rootx()
        widget_y = self.winfo_rooty()
        widget_h = self.winfo_height()
        entry_w = self.entry.winfo_width()
        root_x = self.root_window.winfo_rootx()
        
        # Calculate in logical units
        x = int((entry_x - root_x) / scaling)
        y = int((widget_y - outer_y + widget_h) / scaling) + 2
        
        # Width: Don't divide by scaling - CTk configure uses logical, winfo might already be logical
        dropdown_w = entry_w  # Try without scaling
        dropdown_h = 250
        
        print(f"DEBUG3: scaling={scaling}, entry_w={entry_w}, dropdown_w={dropdown_w}")
        
        # Configure frame size and place it
        self.dropdown_frame.configure(width=dropdown_w, height=dropdown_h)
        self.dropdown_frame.place(x=x, y=y)
        self.dropdown_frame.lift() # Bring to front
        
        # Search Entry
        self.search_var = ctk.StringVar()
        self.search_var.trace("w", self.filter_list)
        search_entry = ctk.CTkEntry(self.dropdown_frame, textvariable=self.search_var, placeholder_text="Search...")
        search_entry.pack(fill="x", padx=5, pady=5)
        search_entry.focus_set()
        
        # Scrollable List
        self.scroll_frame = ctk.CTkScrollableFrame(self.dropdown_frame)
        self.scroll_frame.pack(fill="both", expand=True, padx=5, pady=(0, 5))
        self.populate_list(self.values)
        
        # Bind click-outside to close (optional, simple version: close on focus out of search_entry is tricky)
        # For now, clicking the arrow again will close it.

    def filter_list(self, *args):
        query = self.search_var.get().lower()
        filtered = [v for v in self.values if query in v.lower()]
        self.populate_list(filtered)

    def populate_list(self, items):
        for w in self.scroll_frame.winfo_children(): w.destroy()
        for item in items:
            ctk.CTkButton(self.scroll_frame, text=item, anchor="w", fg_color="transparent", 
                          height=28, command=lambda i=item: self.on_select(i)).pack(fill="x")
            
    def on_select(self, item):
        if self.variable: self.variable.set(item)
        self.toggle_dropdown() # Close dropdown

class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, master, current_config):
        super().__init__(master)
        self.title("Configuration")
        self.geometry("500x600")
        self.config = current_config
        self.attributes("-topmost", True)
        
        self.grid_columnconfigure(1, weight=1)

        # Variables
        self.v_temp = ctk.DoubleVar(value=self.config.get('temperature', 0.2))
        self.v_max_tok = ctk.IntVar(value=self.config.get('max_tokens', 4096))
        self.v_top_p = ctk.DoubleVar(value=self.config.get('top_p', 1.0))
        self.v_top_k = ctk.IntVar(value=self.config.get('top_k', -1))
        self.v_stream = ctk.BooleanVar(value=self.config.get('stream', True))
        
        # UI
        row = 0
        ctk.CTkLabel(self, text="System Prompt:", font=("Arial", 12, "bold")).grid(row=row, column=0, padx=10, pady=(10,0), sticky="nw")
        row+=1
        self.txt_prompt = ctk.CTkTextbox(self, height=150)
        self.txt_prompt.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        self.txt_prompt.insert("0.0", self.config.get('system_prompt', ""))
        
        row+=1
        ctk.CTkLabel(self, text="Temperature:").grid(row=row, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.v_temp).grid(row=row, column=1, padx=10, sticky="ew")
        
        row+=1
        ctk.CTkLabel(self, text="Max Tokens:").grid(row=row, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.v_max_tok).grid(row=row, column=1, padx=10, sticky="ew")

        row+=1
        ctk.CTkLabel(self, text="Top P:").grid(row=row, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.v_top_p).grid(row=row, column=1, padx=10, sticky="ew")

        row+=1
        ctk.CTkLabel(self, text="Top K (-1 to disable):").grid(row=row, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.v_top_k).grid(row=row, column=1, padx=10, sticky="ew")
        
        row+=1
        ctk.CTkSwitch(self, text="Stream Output (Real-time logs)", variable=self.v_stream).grid(row=row, column=0, columnspan=2, padx=10, pady=15)
        
        row+=1
        ctk.CTkButton(self, text="SAVE", command=self.save_and_close, fg_color="green", height=40).grid(row=row, column=0, columnspan=2, padx=20, pady=20, sticky="ew")

    def save_and_close(self):
        # Update config dict
        self.config['system_prompt'] = self.txt_prompt.get("0.0", "end").strip()
        self.config['temperature'] = self.v_temp.get()
        self.config['max_tokens'] = self.v_max_tok.get()
        self.config['top_p'] = self.v_top_p.get()
        self.config['top_k'] = self.v_top_k.get()
        self.config['stream'] = self.v_stream.get()
        
        # Save to file
        global config_data
        config_data = self.config
        save_config()
        self.destroy()

class ThreadProgressWidget(ctk.CTkFrame):
    def __init__(self, master, thread_id, range_info, **kwargs):
        super().__init__(master, height=50, **kwargs)
        self.thread_id = thread_id
        
        # Layout: [ID: 1-100] [|||||| ] [30%] [>]
        self.grid_columnconfigure(1, weight=1)
        
        self.lbl_info = ctk.CTkLabel(self, text=f"[{thread_id}: {range_info}]", width=120, anchor="w")
        self.lbl_info.grid(row=0, column=0, padx=10)
        
        self.progress = ctk.CTkProgressBar(self, height=15)
        self.progress.grid(row=0, column=1, padx=10, sticky="ew")
        self.progress.set(0)
        
        self.lbl_pct = ctk.CTkLabel(self, text="0%", width=40)
        self.lbl_pct.grid(row=0, column=2, padx=5)
        
        self.btn_view = ctk.CTkButton(self, text=">", width=30, command=self.open_monitor)
        self.btn_view.grid(row=0, column=3, padx=(5, 10))
        
        self.log_window = None
        self.log_text = ""
        self.stream_active = False

    def update_progress(self, current, total, log_msg, append=False):
        pct = current / max(1, total)
        self.progress.set(pct)
        self.lbl_pct.configure(text=f"{int(pct*100)}%")
        
        if append:
            self.log_text += log_msg
        elif log_msg:
             self.log_text += f"\n> {log_msg}\n"

        if self.log_window and self.log_window.winfo_exists():
            self.log_window.append_log(log_msg if append else f"\n> {log_msg}\n")

    def open_monitor(self):
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.focus()
        else:
            self.log_window = StreamMonitorWindow(self, self.thread_id, self.log_text)

class StreamMonitorWindow(ctk.CTkToplevel):
    def __init__(self, master, thread_id, initial_text):
        super().__init__(master)
        self.title(f"Thread {thread_id} Stream Monitor")
        self.geometry("600x400")
        
        self.textbox = ctk.CTkTextbox(self, font=("Consolas", 12))
        self.textbox.pack(fill="both", expand=True)
        self.textbox.insert("0.0", initial_text)
        self.textbox.see("end")

    def append_log(self, text):
        self.textbox.insert("end", text)
        self.textbox.see("end")

# --- MAIN APP ---
class MainApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        load_config()
        self.title("WuWa Localization Tool v7")
        self.geometry("600x600")
        self.minsize(500, 600)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1) # Progress area expands

        # VARS
        self.v_base_url = ctk.StringVar(value=config_data.get('base_url', ''))
        self.v_api_key = ctk.StringVar(value=config_data.get('api_key', ''))
        self.v_model = ctk.StringVar(value=config_data.get('model', ''))
        self.v_file = ctk.StringVar(value=config_data.get('last_file', ''))
        
        self.v_threads = ctk.IntVar(value=config_data.get('threads', 1))
        self.v_batch = ctk.IntVar(value=config_data.get('batch_size', 50))
        self.v_delay = ctk.DoubleVar(value=config_data.get('delay', 1.3))

        self.setup_ui()
        self.is_running = False

    def setup_ui(self):
        # 1. HEADER & SETTINGS
        head_frame = ctk.CTkFrame(self, fg_color="transparent")
        head_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 10))
        
        ctk.CTkLabel(head_frame, text="WUWA LOCALIZER", font=("Arial", 20, "bold")).pack(side="left")
        
        btn_settings = ctk.CTkButton(head_frame, text="⚙ Settings", width=80, command=self.open_settings)
        btn_settings.pack(side="right")

        # 2. CONFIG AREA
        conf_frame = ctk.CTkFrame(self)
        conf_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=5)
        conf_frame.grid_columnconfigure(1, weight=1)
        
        # URL
        ctk.CTkLabel(conf_frame, text="URL:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(conf_frame, textvariable=self.v_base_url).grid(row=0, column=1, padx=10, sticky="ew")
        
        # KEY
        ctk.CTkLabel(conf_frame, text="Key:").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(conf_frame, textvariable=self.v_api_key, show="*").grid(row=1, column=1, padx=10, sticky="ew")
        
        # MODEL
        ctk.CTkLabel(conf_frame, text="Model:").grid(row=2, column=0, padx=10, pady=10, sticky="w")
        mod_box = ctk.CTkFrame(conf_frame, fg_color="transparent")
        mod_box.grid(row=2, column=1, sticky="ew", padx=5)
        mod_box.grid_columnconfigure(0, weight=1)
        
        self.cb_model = SearchableComboBox(mod_box, variable=self.v_model, load_command=self.lazy_load_models)
        self.cb_model.grid(row=0, column=0, sticky="ew", padx=5)
        ctk.CTkButton(mod_box, text="↻", width=30, command=self.load_models).grid(row=0, column=1)

        # ARGS (Threads, Batch, Delay)
        arg_frame = ctk.CTkFrame(conf_frame, fg_color="#333")
        arg_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=10)
        arg_frame.grid_columnconfigure((0,1,2), weight=1)
        
        ctk.CTkLabel(arg_frame, text="Threads").grid(row=0, column=0)
        ctk.CTkLabel(arg_frame, text="Batch").grid(row=0, column=1)
        ctk.CTkLabel(arg_frame, text="Delay(s)").grid(row=0, column=2)
        
        ctk.CTkEntry(arg_frame, textvariable=self.v_threads, width=50, justify="center").grid(row=1, column=0, pady=5)
        ctk.CTkEntry(arg_frame, textvariable=self.v_batch, width=60, justify="center").grid(row=1, column=1, pady=5)
        ctk.CTkEntry(arg_frame, textvariable=self.v_delay, width=50, justify="center").grid(row=1, column=2, pady=5)
        
        # FILE
        ctk.CTkLabel(conf_frame, text="File:").grid(row=4, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(conf_frame, textvariable=self.v_file).grid(row=4, column=1, padx=(10,0), sticky="ew")
        ctk.CTkButton(conf_frame, text="📂", width=40, command=self.browse_file).grid(row=4, column=2, padx=10)
        
        # 3. ACTION BUTTONS
        act_frame = ctk.CTkFrame(self, fg_color="transparent")
        act_frame.grid(row=2, column=0, sticky="ew", padx=20, pady=10)
        act_frame.grid_columnconfigure(0, weight=1)
        act_frame.grid_columnconfigure(1, weight=1)
        
        self.btn_start = ctk.CTkButton(act_frame, text="START TRANSLATING", height=40, fg_color="#27ae60", command=self.start_process)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0,5))
        
        self.btn_stop = ctk.CTkButton(act_frame, text="STOP", height=40, fg_color="#c0392b", state="disabled", command=self.stop_process)
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(5,0))

        # 4. PROGRESS AREA (Initially Hidden/Empty)
        self.scroll_progress = ctk.CTkScrollableFrame(self)
        self.scroll_progress.grid(row=3, column=0, sticky="nsew", padx=20, pady=10)
        
    def open_settings(self):
        SettingsWindow(self, config_data)

    def browse_file(self):
        f = filedialog.askopenfilename()
        if f: self.v_file.set(f)

    def lazy_load_models(self):
        if not self.cb_model.values:
            self.load_models()

    def load_models(self):
        url = self.v_base_url.get().rstrip('/')
        key = self.v_api_key.get()
        if not url: return
        
        def run():
            try:
                r = requests.get(f"{url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    lst = [m['id'] for m in data['data']] if 'data' in data else [str(m) for m in data]
                    lst.sort()
                    self.cb_model.set_values(lst)
            except: pass
        threading.Thread(target=run, daemon=True).start()

    def stop_process(self):
        if self.is_running:
            stop_event.set()
            self.btn_stop.configure(state="disabled")

    def toggle_inputs(self, enable):
        s = "normal" if enable else "disabled"
        self.btn_start.configure(state=s)
        self.btn_stop.configure(state="normal" if not enable else "disabled")

    def start_process(self):
        f_in = self.v_file.get()
        if not os.path.exists(f_in):
            messagebox.showerror("Error", "File not found")
            return
            
        # Save current UI settings to config
        config_data.update({
            'base_url': self.v_base_url.get(),
            'api_key': self.v_api_key.get(),
            'model': self.v_model.get(),
            'threads': self.v_threads.get(),
            'batch_size': self.v_batch.get(),
            'delay': self.v_delay.get(),
            'last_file': f_in
        })
        save_config()

        self.input_path = f_in
        self.is_running = True
        stop_event.clear()
        self.toggle_inputs(False)

        # Clear old progress widgets
        for w in self.scroll_progress.winfo_children(): w.destroy()
        
        threading.Thread(target=self.run_logic).start()

    def run_logic(self):
        global shared_output_lines
        shared_output_lines = []
        
        try:
            with open(self.input_path, 'r', encoding='utf-8') as f:
                raw = [l.strip() for l in f.readlines()]
            
            # Init shared lines
            has_header = raw and raw[0].startswith("0:::")
            shared_output_lines = []
            process_data = [] # List of (line, global_index)
            
            # Simple init
            for i, line in enumerate(raw):
                if ':::' in line:
                    o_id = line.split(':::')[0].strip()
                    shared_output_lines.append(f"{o_id}:::")
                else:
                    shared_output_lines.append(line)
            
            start_off = 1 if has_header else 0
            # Data for workers
            data_points = []
            for i, line in enumerate(raw[start_off:]):
                 data_points.append((line, start_off + i))
            
            total = len(data_points)
            n_threads = max(1, config_data['threads'])
            chunk_size = math.ceil(total / n_threads)
            
            chunks = []
            for i in range(0, total, chunk_size):
                chunks.append(data_points[i:i+chunk_size])
            chunks = [c for c in chunks if c]
            
            # Create widgets in Main Thread
            self.thread_widgets = []
            for i, chunk in enumerate(chunks):
                first_idx = chunk[0][1]
                last_idx = chunk[-1][1]
                w = ThreadProgressWidget(self.scroll_progress, i+1, f"{first_idx}-{last_idx}")
                w.pack(fill="x", pady=2)
                self.thread_widgets.append(w)
            
            threads = []
            for i, chunk in enumerate(chunks):
                w_widget = self.thread_widgets[i]
                t = threading.Thread(target=worker_process, 
                                     args=(i+1, chunk, config_data, w_widget.update_progress))
                threads.append(t)
                t.start()
            
            for t in threads: t.join()
            
            if not stop_event.is_set():
                if os.path.exists(OUTPUT_FILE): os.remove(OUTPUT_FILE)
                shutil.copy(TEMP_OUTPUT_FILE, OUTPUT_FILE)
                messagebox.showinfo("Done", f"Finished. {OUTPUT_FILE}")

        except Exception as e:
            print(e)
        finally:
            self.is_running = False
            self.toggle_inputs(True)

if __name__ == "__main__":
    MainApp().mainloop()