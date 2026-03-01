import customtkinter as ctk
import subprocess
import threading
import sys
import queue
import time
import socket
import os
import yaml

# --- Config & Theme ---
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class HydroGuardLauncher(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("HYDROGUARD - AI Flood Detection Launcher")
        self.geometry("900x600")
        self.minsize(800, 500)
        
        # Grid layout (1 row, 2 columns)
        self.grid_columnconfigure(0, weight=1) # Control Panel
        self.grid_columnconfigure(1, weight=3) # Terminal Panel
        self.grid_rowconfigure(0, weight=1)

        # --------------------------------
        # Left Panel (Controls & Status)
        # --------------------------------
        self.left_frame = ctk.CTkFrame(self, corner_radius=0)
        self.left_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        
        self.logo_label = ctk.CTkLabel(
            self.left_frame, 
            text="HYDROGUARD\nController", 
            font=ctk.CTkFont(size=20, weight="bold")
        )
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        # --- Diagnostics Frame ---
        self.diag_frame = ctk.CTkFrame(self.left_frame)
        self.diag_frame.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        
        ctk.CTkLabel(self.diag_frame, text="System Diagnostics", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=2, pady=5)
        
        # Python Venv Check
        self.lbl_venv = ctk.CTkLabel(self.diag_frame, text="Checking VENV...")
        self.lbl_venv.grid(row=1, column=0, sticky="w", padx=10)
        
        # Config Check
        self.lbl_config = ctk.CTkLabel(self.diag_frame, text="Checking Config...")
        self.lbl_config.grid(row=2, column=0, sticky="w", padx=10)
        
        # Port Check
        self.lbl_port = ctk.CTkLabel(self.diag_frame, text="Checking Port 5000...")
        self.lbl_port.grid(row=3, column=0, sticky="w", padx=10)

        # --- Controls ---
        self.btn_start = ctk.CTkButton(
            self.left_frame, text="START ENGINE", 
            fg_color="green", hover_color="darkgreen",
            command=self.start_engine, height=40
        )
        self.btn_start.grid(row=2, column=0, padx=20, pady=(20, 10), sticky="ew")

        self.btn_stop = ctk.CTkButton(
            self.left_frame, text="STOP ENGINE", 
            fg_color="red", hover_color="darkred",
            command=self.stop_engine, height=40, state="disabled"
        )
        self.btn_stop.grid(row=3, column=0, padx=20, pady=10, sticky="ew")
        
        self.btn_dashboard = ctk.CTkButton(
            self.left_frame, text="Open Web Dashboard", 
            command=self.open_dashboard, height=40, state="disabled"
        )
        self.btn_dashboard.grid(row=4, column=0, padx=20, pady=10, sticky="ew")

        # --------------------------------
        # Right Panel (Terminal)
        # --------------------------------
        self.right_frame = ctk.CTkFrame(self)
        self.right_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.right_frame.grid_columnconfigure(0, weight=1)
        self.right_frame.grid_rowconfigure(0, weight=1)

        self.terminal = ctk.CTkTextbox(
            self.right_frame, 
            font=ctk.CTkFont(family="monospace", size=12),
            text_color="#00ff00", fg_color="#000000"
        )
        self.terminal.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.terminal.insert("0.0", "--- HYDROGUARD Terminal Initialized ---\n")
        self.terminal.configure(state="disabled")

        # --- State Variables ---
        self.process = None
        self.queue = queue.Queue()
        self.running = False
        
        # Run diagnostics immediately
        self.run_diagnostics()
        
        # Start queue reader loop (100ms)
        self.after(100, self.process_queue)

    def log(self, message):
        """Thread-safe terminal print"""
        self.queue.put(message)

    def process_queue(self):
        """Main thread loop checking for new terminal messages"""
        while not self.queue.empty():
            msg = self.queue.get()
            self.terminal.configure(state="normal")
            self.terminal.insert("end", msg + "\n")
            self.terminal.see("end")  # Auto scroll
            self.terminal.configure(state="disabled")
        
        # Keep looping
        self.after(100, self.process_queue)

    # ==========================
    # Diagnostic Checks 
    # ==========================
    def run_diagnostics(self):
        self.log("Running Pre-flight System Checks...")
        
        # 1. Check Virtual Environment
        is_venv = hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
        if is_venv:
            self.lbl_venv.configure(text="VEnv: OK", text_color="green")
            self.log("[✓] Virtual Environment active")
        else:
            self.lbl_venv.configure(text="VEnv: MISMATCH", text_color="red")
            self.log("[x] Virtual Environment not active! Scripts may fail.")
            
        # 2. Check config.yaml
        if os.path.exists("config.yaml"):
            try:
                with open("config.yaml", "r") as f:
                    yaml.safe_load(f)
                self.lbl_config.configure(text="Config: OK", text_color="green")
                self.log("[✓] config.yaml validated")
            except Exception as e:
                self.lbl_config.configure(text="Config: CORRUPTED", text_color="red")
                self.log(f"[x] Config error: {e}")
        else:
             self.lbl_config.configure(text="Config: MISSING", text_color="red")
             self.log("[x] config.yaml not found!")
             
        # 3. Check Port 5000 (Is Dashboard Already Running?)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 5000))
        sock.close()
        if result == 0:
            self.lbl_port.configure(text="Port 5000: IN USE", text_color="orange")
            self.log("[!] Port 5000 is already in use. Is the engine already running?")
        else:
            self.lbl_port.configure(text="Port 5000: FREE", text_color="green")
            self.log("[✓] Port 5000 available for Dashboard")

        self.log("--- Diagnostics Complete ---\n")

    # ==========================
    # Process Management
    # ==========================
    def read_stdpipe(self, pipe, is_error=False):
        """Thread worker to read process output continuously"""
        for line in iter(pipe.readline, ''):
            if not line: break
            self.log(line.strip())
        pipe.close()

    def start_engine(self):
        if self.process is not None and self.process.poll() is None:
            return # Already running
            
        self.log(">>> STARTING HYDROGUARD ENGINE...")
        
        # Determine python executable (prefer venv)
        python_exe = sys.executable
        if os.path.exists(os.path.join(".venv", "bin", "python")):
            python_exe = os.path.join(".venv", "bin", "python")
            
        try:
            # Spawn subprocess
            self.process = subprocess.Popen(
                [python_exe, "flood_system/app.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, # Merge stderr into stdout
                text=True,
                bufsize=1,
                universal_newlines=True,
                cwd=os.getcwd()
            )
            
            # Start background reader thread
            threading.Thread(target=self.read_stdpipe, args=(self.process.stdout,), daemon=True).start()
            
            # Update GUI state
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.btn_dashboard.configure(state="normal")
            self.lbl_port.configure(text="Port 5000: ACTIVE", text_color="green")
            
            # Monitor process life
            self.after(1000, self.monitor_process)
            
        except Exception as e:
            self.log(f"[ERROR] Failed to launch: {e}")

    def monitor_process(self):
        """Check if process died unexpectedly"""
        if self.process is not None:
            ret_code = self.process.poll()
            if ret_code is not None:
                self.log(f"\n<<< ENGINE TERMINATED (Code: {ret_code}) <<<")
                self.process = None
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.btn_dashboard.configure(state="disabled")
                self.lbl_port.configure(text="Port 5000: FREE", text_color="green")
            else:
                # Still alive, keep polling
                self.after(1000, self.monitor_process)

    def stop_engine(self):
        if self.process is not None:
            self.log(">>> SENDING STOP SIGNAL...")
            self.process.terminate() # SIGTERM
            
            # Allow 3 seconds to die gracefully, then SIGKILL
            for _ in range(30):
                if self.process.poll() is not None: break
                time.sleep(0.1)
                
            if self.process.poll() is None:
                self.log(">>> FORCE KILLING ENGINE...")
                self.process.kill()
                
            # Process state resets in monitor_process tick automatically

    def open_dashboard(self):
        """Launch user's default web browser"""
        import webbrowser
        self.log("Opening Web Dashboard: http://localhost:5000")
        webbrowser.open("http://localhost:5000")

    def on_closing(self):
        """Window generic close hook"""
        self.stop_engine()
        self.destroy()

if __name__ == "__main__":
    app = HydroGuardLauncher()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
