import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import os
import json
import shutil
import tempfile
import matplotlib.pyplot as plt
from itertools import product, combinations
import time
import sys
import argparse
import traceback
import threading
import queue

APP_SETTINGS_FILENAME = "vina_batch_settings.json"


class RunCancelled(Exception):
    pass


class RunStoppedAfterReceptor(Exception):
    pass


# ======================================================
# App / packaging helpers
# ======================================================

def app_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def user_working_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def settings_path():
    return os.path.join(user_working_dir(), APP_SETTINGS_FILENAME)


def launchable_program():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def patch_pybel_for_plip():
    try:
        from openbabel import pybel
    except Exception:
        return

    original_write = pybel.Molecule.write

    def safe_write(self, format='smi', filename=None, overwrite=False, opt=None):
        fmt = str(format).lower().strip()
        if fmt == "inchikey":
            return ""
        return original_write(self, format=format, filename=filename, overwrite=overwrite, opt=opt)

    pybel.Molecule.write = safe_write


def run_internal_plip_worker():
    patch_pybel_for_plip()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plip-worker", action="store_true")
    parser.add_argument("-f", "--file", dest="input_file", required=True)
    parser.add_argument("-o", "--out", dest="out_dir", required=True)
    parser.add_argument("-t", action="store_true")
    parser.add_argument("-x", action="store_true")
    parser.add_argument("-v", action="store_true")
    args, unknown = parser.parse_known_args()

    sys.argv = [
        sys.argv[0],
        "-f", args.input_file,
        "-o", args.out_dir,
    ]
    if args.t:
        sys.argv.append("-t")
    if args.x:
        sys.argv.append("-x")
    if args.v:
        sys.argv.append("-v")
    sys.argv.extend(unknown)

    from plip.plipcmd import main as plip_main
    return plip_main()


# ======================================================
# Startup self-check helpers
# ======================================================

def try_import(module_name):
    try:
        __import__(module_name)
        return True, ""
    except Exception as e:
        return False, str(e)


def check_vina_available(explicit_path=""):
    candidates = []

    if explicit_path:
        candidates.append(explicit_path)

    for base in [app_dir(), user_working_dir()]:
        candidates.append(os.path.join(base, "vina.exe"))
        candidates.append(os.path.join(base, "tools", "vina.exe"))
        candidates.append(os.path.join(base, "bin", "vina.exe"))

    for p in candidates:
        if p and os.path.exists(p):
            try:
                result = subprocess.run(
                    [p, "--help"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10
                )
                return True, p, result.returncode
            except Exception as e:
                return False, p, str(e)

    found = shutil.which("vina")
    if found:
        try:
            result = subprocess.run(
                [found, "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )
            return True, found, result.returncode
        except Exception as e:
            return False, found, str(e)

    return False, "", "vina executable not found"


def check_plip_stack():
    report = []

    ok_plip, err_plip = try_import("plip")
    report.append(("PLIP import", ok_plip, err_plip))

    ok_ob, err_ob = try_import("openbabel")
    report.append(("OpenBabel import", ok_ob, err_ob))

    ok_pybel = False
    err_pybel = ""
    try:
        from openbabel import pybel  # noqa: F401
        ok_pybel = True
    except Exception as e:
        err_pybel = str(e)

    report.append(("Pybel import", ok_pybel, err_pybel))
    return report


def format_self_check_report(vina_field_value=""):
    lines = []
    lines.append("VLIP startup self-check")
    lines.append("")

    vina_ok, vina_path, vina_info = check_vina_available(vina_field_value)
    lines.append(f"Vina: {'OK' if vina_ok else 'FAIL'}")
    if vina_path:
        lines.append(f"Path: {vina_path}")
    if vina_info != 0 and vina_info != "vina executable not found":
        lines.append(f"Detail: {vina_info}")
    lines.append("")

    for label, ok, detail in check_plip_stack():
        lines.append(f"{label}: {'OK' if ok else 'FAIL'}")
        if detail and not ok:
            lines.append(f"Detail: {detail}")
        lines.append("")

    return "\n".join(lines), vina_ok


# ======================================================
# Diagnostics helpers
# ======================================================

def log_line(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(text.rstrip() + "\n")


def warn_path_len(label, path, warn_at=240):
    if os.name == "nt":
        try:
            n = len(os.path.abspath(path))
        except Exception:
            n = len(path)
        if n >= warn_at:
            return f"[WARN] {label} path is long ({n} chars): {path}"
    return None


def wait_for_stable_file(path, timeout_s=10.0, stable_window_s=0.5, cancel_event=None):
    t0 = time.time()
    last_size = None
    stable_since = None

    while time.time() - t0 < timeout_s:
        if cancel_event is not None and cancel_event.is_set():
            raise RunCancelled("Cancelled by user.")

        if os.path.exists(path):
            try:
                sz = os.path.getsize(path)
            except OSError:
                sz = None

            if sz is not None and sz == last_size:
                if stable_since is None:
                    stable_since = time.time()
                elif (time.time() - stable_since) >= stable_window_s:
                    return True
            else:
                stable_since = None
                last_size = sz

        time.sleep(0.1)
    return False


def validate_pdbqt(path, kind="file", max_atom_lines=2000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            atom_lines = [l.rstrip("\n") for l in f if l.startswith(("ATOM", "HETATM"))]

        if not atom_lines:
            return False, f"{kind}: no ATOM/HETATM lines"

        bad_coord = 0
        for l in atom_lines[:max_atom_lines]:
            if "\t" in l:
                return False, f"{kind}: contains TAB characters (Vina often hates tabs)"
            try:
                float(l[30:38])
                float(l[38:46])
                float(l[46:54])
            except Exception:
                bad_coord += 1
                if bad_coord > 5:
                    return False, f"{kind}: non-numeric coordinates / misaligned columns"

            parts = l.split()
            if len(parts) < 2:
                return False, f"{kind}: ATOM line too short"
            try:
                float(parts[-2])
            except Exception:
                return False, f"{kind}: missing/invalid partial charge (2nd last token)"

        return True, "OK"
    except Exception as ex:
        return False, f"{kind}: cannot read ({ex})"


# ======================================================
# Geometry helpers
# ======================================================

def parse_atoms(pdbqt):
    atoms = []
    with open(pdbqt, "r", encoding="utf-8", errors="replace") as f:
        for l in f:
            if l.startswith(("ATOM", "HETATM")):
                try:
                    x = float(l[30:38])
                    y = float(l[38:46])
                    z = float(l[46:54])
                    res = int(l[22:26].strip())
                    atoms.append((x, y, z, res))
                except Exception:
                    pass
    return atoms


def box_from_atoms(atoms, padding):
    xs = [a[0] for a in atoms]
    ys = [a[1] for a in atoms]
    zs = [a[2] for a in atoms]

    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    minz, maxz = min(zs), max(zs)

    cx = (minx + maxx) / 2
    cy = (miny + maxy) / 2
    cz = (minz + maxz) / 2

    sx = (maxx - minx) + padding
    sy = (maxy - miny) + padding
    sz = (maxz - minz) + padding

    return cx, cy, cz, sx, sy, sz, minx, maxx, miny, maxy, minz, maxz


# ======================================================
# Progress overlay
# ======================================================

class ProgressOverlay:
    def __init__(self, parent, pause_callback, cancel_callback, stop_after_receptor_callback):
        self.parent = parent
        self.pause_callback = pause_callback
        self.cancel_callback = cancel_callback
        self.stop_after_receptor_callback = stop_after_receptor_callback

        self.win = tk.Toplevel(parent)
        self.win.title("Docking in progress")
        self.win.geometry("720x390")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)

        self.status_var = tk.StringVar(value="Starting...")
        self.count_var = tk.StringVar(value="0 / 0")
        self.ligand_var = tk.StringVar(value="Ligand: ")
        self.receptor_var = tk.StringVar(value="Receptor: ")
        self.run_var = tk.StringVar(value="Run: ")
        self.stop_state_var = tk.StringVar(value="Stop-after-receptor: not requested")

        frame = tk.Frame(self.win, padx=20, pady=20)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="Docking batch running",
            font=("Segoe UI", 12, "bold")
        ).pack(anchor="w", pady=(0, 10))

        tk.Label(
            frame,
            textvariable=self.status_var,
            font=("Segoe UI", 10)
        ).pack(anchor="w", pady=(0, 8))

        self.pb = ttk.Progressbar(frame, mode="determinate", length=650)
        self.pb.pack(fill="x", pady=(0, 10))

        tk.Label(frame, textvariable=self.count_var).pack(anchor="w", pady=(0, 8))
        tk.Label(frame, textvariable=self.ligand_var, justify="left", wraplength=650).pack(anchor="w", pady=2)
        tk.Label(frame, textvariable=self.receptor_var, justify="left", wraplength=650).pack(anchor="w", pady=2)
        tk.Label(frame, textvariable=self.run_var, justify="left", wraplength=650).pack(anchor="w", pady=2)
        tk.Label(frame, textvariable=self.stop_state_var, justify="left", wraplength=650).pack(anchor="w", pady=(8, 2))

        tk.Label(
            frame,
            text="Pause waits for the current subprocess to finish. Stop After Receptor finishes the current receptor's repeats, then stops cleanly.",
            fg="gray",
            wraplength=650,
            justify="left"
        ).pack(anchor="w", pady=(14, 8))

        btns = tk.Frame(frame)
        btns.pack(fill="x", pady=(4, 0))

        self.pause_btn = tk.Button(btns, text="Pause", width=14, command=self.pause_callback)
        self.pause_btn.pack(side="left", padx=(0, 8))

        self.stop_after_receptor_btn = tk.Button(
            btns,
            text="Stop After Receptor",
            width=18,
            command=self.stop_after_receptor_callback
        )
        self.stop_after_receptor_btn.pack(side="left", padx=(0, 8))

        self.cancel_btn = tk.Button(btns, text="Cancel Run", width=14, command=self.cancel_callback)
        self.cancel_btn.pack(side="left")

        self.center()
        self.win.withdraw()
        self.win.deiconify()
        self.win.lift()
        self.win.update()

    def center(self):
        self.parent.update_idletasks()
        pw = self.parent.winfo_width()
        ph = self.parent.winfo_height()
        px = self.parent.winfo_rootx()
        py = self.parent.winfo_rooty()

        ww = 720
        wh = 390
        x = px + max((pw - ww) // 2, 0)
        y = py + max((ph - wh) // 2, 0)
        self.win.geometry(f"{ww}x{wh}+{x}+{y}")

    def update_progress(self, current, total, status="", ligand="", receptor="", run_text=""):
        total = max(total, 1)
        self.pb["maximum"] = total
        self.pb["value"] = current
        self.count_var.set(f"{current} / {total}")
        self.status_var.set(status)
        self.ligand_var.set(f"Ligand: {ligand}")
        self.receptor_var.set(f"Receptor: {receptor}")
        self.run_var.set(f"Run: {run_text}")

    def set_paused(self, paused):
        self.pause_btn.config(text="Resume" if paused else "Pause")

    def set_cancelled_state(self):
        self.pause_btn.config(state="disabled")
        self.stop_after_receptor_btn.config(state="disabled")
        self.cancel_btn.config(state="disabled")

    def set_stop_after_receptor_requested(self, requested):
        if requested:
            self.stop_state_var.set("Stop-after-receptor: requested")
            self.stop_after_receptor_btn.config(text="Stop Requested", state="disabled")
        else:
            self.stop_state_var.set("Stop-after-receptor: not requested")
            self.stop_after_receptor_btn.config(text="Stop After Receptor", state="normal")

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ======================================================
# Main GUI
# ======================================================

class VinaBatchGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Vina Batch Docking Tool")

        self.receptors = []
        self.ligands = []

        self.job_queue = queue.Queue()
        self.worker_thread = None
        self.overlay = None
        self.run_in_progress = False

        self.pause_event = threading.Event()
        self.cancel_event = threading.Event()
        self.stop_after_receptor_event = threading.Event()

        self.current_process = None
        self.current_process_lock = threading.Lock()

        self.load_settings()
        self.build_ui()
        self.refresh_table()
        self.refresh_ligand_table()

        self.root.after(200, self.startup_self_check)

    # ----------------------
    # Path / bundled resource helpers
    # ----------------------
    def user_working_dir(self):
        return user_working_dir()

    def find_tool(self, *names):
        candidates = []
        bases = [app_dir(), self.user_working_dir()]
        for base in bases:
            for name in names:
                candidates.append(os.path.join(base, name))
                candidates.append(os.path.join(base, "tools", name))
                candidates.append(os.path.join(base, "bin", name))

        for c in candidates:
            if os.path.exists(c):
                return c

        for name in names:
            found = shutil.which(name)
            if found:
                return found

        return ""

    def resolved_vina_path(self):
        typed = self.vina_var.get().strip()
        if typed and os.path.exists(typed):
            return typed
        return self.find_tool("vina.exe", "vina")

    # ----------------------
    # Settings
    # ----------------------
    def load_settings(self):
        self.settings = {}
        sp = settings_path()
        if os.path.exists(sp):
            try:
                with open(sp, "r", encoding="utf-8", errors="replace") as f:
                    self.settings = json.load(f)
            except Exception:
                self.settings = {}

        self.receptors = self.settings.get("receptors", [])
        self.ligands = self.settings.get("ligands", [])

    def save_settings(self):
        self.settings.update({
            "ligand": self.ligand_var.get(),
            "ligands": self.ligands,
            "vina": self.vina_var.get(),
            "outdir": self.outdir_var.get(),
            "padding": self.padding_var.get(),
            "repeats": self.repeat_var.get(),
            "exhaust": self.exhaust_var.get(),
            "merge": self.merge_var.get(),
            "pdb": self.pdb_var.get(),
            "txt": self.txt_var.get(),
            "run_plip": self.plip_var.get(),
            "receptors": self.receptors
        })
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    # ----------------------
    # UI
    # ----------------------
    def build_ui(self):
        top = tk.Frame(self.root)
        top.pack(fill="x", pady=5)

        for txt, cmd in [
            ("Add receptor", self.add_receptor),
            ("Remove", self.remove_receptor),
            ("Move Up", self.move_up),
            ("Move Down", self.move_down),
            ("Select Output Folder", self.pick_outdir),
            ("Diagnose", self.run_diagnostics),
        ]:
            tk.Button(top, text=txt, command=cmd).pack(side="left")

        self.table = ttk.Treeview(self.root, columns=("name", "mode", "res", "padding"), show="headings", height=6)
        for c, t in zip(("name", "mode", "res", "padding"), ("Receptor", "Mode", "Residues", "Padding")):
            self.table.heading(c, text=t)
        self.table.pack(fill="x", pady=5)
        self.table.bind("<Double-1>", self.edit_row)

        cfg = tk.Frame(self.root)
        cfg.pack(fill="x")

        self.ligand_var = tk.StringVar(value=self.settings.get("ligand", ""))
        self.vina_var = tk.StringVar(value=self.settings.get("vina", ""))
        self.outdir_var = tk.StringVar(value=self.settings.get("outdir", ""))
        self.padding_var = tk.StringVar(value=self.settings.get("padding", "10"))
        self.repeat_var = tk.StringVar(value=self.settings.get("repeats", "1"))
        self.exhaust_var = tk.StringVar(value=self.settings.get("exhaust", "8"))

        self.merge_var = tk.BooleanVar(value=self.settings.get("merge", True))
        self.pdb_var = tk.BooleanVar(value=self.settings.get("pdb", True))
        self.txt_var = tk.BooleanVar(value=self.settings.get("txt", False))
        self.plip_var = tk.BooleanVar(value=self.settings.get("run_plip", False))

        def row(r, label, var, btn=None):
            tk.Label(cfg, text=label).grid(row=r, column=0, sticky="w")
            tk.Entry(cfg, textvariable=var, width=55).grid(row=r, column=1, sticky="we")
            if btn:
                tk.Button(cfg, text="Browse", command=btn).grid(row=r, column=2)

        row(0, "Ligand", self.ligand_var, self.pick_ligand)
        row(1, "Vina", self.vina_var, self.pick_vina)
        row(2, "Default Padding", self.padding_var)
        row(3, "Repeats", self.repeat_var)
        row(4, "Exhaustiveness", self.exhaust_var)

        tk.Checkbutton(cfg, text="Merge receptor+ligand (merged.pdbqt)", variable=self.merge_var).grid(row=5, column=0, columnspan=3, sticky="w")
        tk.Checkbutton(cfg, text="Convert to PDB", variable=self.pdb_var).grid(row=6, column=0, columnspan=3, sticky="w")
        tk.Checkbutton(cfg, text="Save per-run summary (.txt)", variable=self.txt_var).grid(row=7, column=0, columnspan=3, sticky="w")
        tk.Checkbutton(cfg, text="Run PLIP on PDB output", variable=self.plip_var).grid(row=8, column=0, columnspan=3, sticky="w")

        ligand_frame = tk.LabelFrame(self.root, text="Ligand Queue")
        ligand_frame.pack(fill="both", expand=False, pady=8, padx=2)

        ligand_btns = tk.Frame(ligand_frame)
        ligand_btns.pack(fill="x", pady=4)

        tk.Button(ligand_btns, text="Add current ligand", command=self.add_current_ligand).pack(side="left")
        tk.Button(ligand_btns, text="Add ligand(s)", command=self.add_ligands).pack(side="left")
        tk.Button(ligand_btns, text="Remove", command=self.remove_ligand).pack(side="left")
        tk.Button(ligand_btns, text="Move Up", command=self.move_ligand_up).pack(side="left")
        tk.Button(ligand_btns, text="Move Down", command=self.move_ligand_down).pack(side="left")
        tk.Button(ligand_btns, text="Clear Queue", command=self.clear_ligands).pack(side="left")

        self.ligand_table = ttk.Treeview(
            ligand_frame,
            columns=("name", "path"),
            show="headings",
            height=6
        )
        self.ligand_table.heading("name", text="Ligand")
        self.ligand_table.heading("path", text="Path")
        self.ligand_table.column("name", width=180, anchor="w")
        self.ligand_table.column("path", width=700, anchor="w")
        self.ligand_table.pack(fill="x", pady=4)

        tk.Label(
            ligand_frame,
            text="If queue is not empty, RUN DOCKING uses the queued ligands. If queue is empty, it uses the single Ligand field above."
        ).pack(anchor="w", padx=4, pady=(0, 4))

        prev = tk.Frame(self.root)
        prev.pack(pady=5)
        tk.Button(prev, text="Preview", command=self.preview).pack(side="left", padx=5)

        self.run_button = tk.Button(
            self.root,
            text="RUN DOCKING",
            bg="green",
            fg="white",
            height=2,
            command=self.run_docking
        )
        self.run_button.pack(fill="x", pady=10)

    # ----------------------
    # Startup checks / diagnostics
    # ----------------------
    def startup_self_check(self):
        report, _ = format_self_check_report(self.vina_var.get().strip())

        if "FAIL" in report:
            messagebox.showwarning(
                "Startup check",
                report + "\n\nThe app can still open, but some features may fail until this is fixed."
            )

    def run_diagnostics(self):
        report, _ = format_self_check_report(self.vina_var.get().strip())

        diag_file = os.path.join(user_working_dir(), "vlip_diagnostics.txt")
        try:
            with open(diag_file, "w", encoding="utf-8") as f:
                f.write(report + "\n")
            messagebox.showinfo(
                "Diagnostics",
                report + f"\n\nSaved to:\n{diag_file}"
            )
        except Exception:
            messagebox.showinfo("Diagnostics", report)

    # ----------------------
    # Pickers
    # ----------------------
    def pick_outdir(self):
        d = filedialog.askdirectory()
        if d:
            self.outdir_var.set(d)

    def pick_ligand(self):
        f = filedialog.askopenfilename(filetypes=[("PDBQT", "*.pdbqt")])
        if f:
            self.ligand_var.set(f)

    def pick_vina(self):
        f = filedialog.askopenfilename()
        if f:
            self.vina_var.set(f)

    # ----------------------
    # Ligand queue helpers
    # ----------------------
    def add_ligand_path(self, path):
        if not path:
            return
        norm = os.path.abspath(path)
        for existing in self.ligands:
            if os.path.abspath(existing) == norm:
                return
        self.ligands.append(path)

    def add_current_ligand(self):
        ligand = self.ligand_var.get().strip()
        if not ligand:
            messagebox.showwarning("No ligand", "Pick a ligand first or type one in the Ligand field.")
            return
        if not os.path.exists(ligand):
            messagebox.showerror("Missing ligand", f"Ligand not found:\n{ligand}")
            return
        self.add_ligand_path(ligand)
        self.refresh_ligand_table()
        self.save_settings()

    def add_ligands(self):
        files = filedialog.askopenfilenames(filetypes=[("PDBQT", "*.pdbqt")])
        for f in files:
            self.add_ligand_path(f)
        self.refresh_ligand_table()
        self.save_settings()

    def remove_ligand(self):
        sel = self.ligand_table.selection()
        if not sel:
            return
        remove_paths = set(sel)
        self.ligands = [p for p in self.ligands if p not in remove_paths]
        self.refresh_ligand_table()
        self.save_settings()

    def move_ligand_up(self):
        sel = self.ligand_table.selection()
        for s in sel:
            i = next((idx for idx, p in enumerate(self.ligands) if p == s), None)
            if i is not None and i > 0:
                self.ligands[i], self.ligands[i - 1] = self.ligands[i - 1], self.ligands[i]
        self.refresh_ligand_table()
        for s in sel:
            if self.ligand_table.exists(s):
                self.ligand_table.selection_add(s)
        self.save_settings()

    def move_ligand_down(self):
        sel = self.ligand_table.selection()
        for s in reversed(sel):
            i = next((idx for idx, p in enumerate(self.ligands) if p == s), None)
            if i is not None and i < len(self.ligands) - 1:
                self.ligands[i], self.ligands[i + 1] = self.ligands[i + 1], self.ligands[i]
        self.refresh_ligand_table()
        for s in sel:
            if self.ligand_table.exists(s):
                self.ligand_table.selection_add(s)
        self.save_settings()

    def clear_ligands(self):
        self.ligands = []
        self.refresh_ligand_table()
        self.save_settings()

    def refresh_ligand_table(self):
        self.ligand_table.delete(*self.ligand_table.get_children())
        for lig in self.ligands:
            self.ligand_table.insert(
                "",
                "end",
                iid=lig,
                values=(os.path.basename(lig), lig)
            )

    # ----------------------
    # Receptor table
    # ----------------------
    def add_receptor(self):
        for p in filedialog.askopenfilenames(filetypes=[("PDBQT", "*.pdbqt")]):
            r = {
                "name": os.path.basename(p),
                "path": p,
                "mode": "Whole",
                "residues": "",
                "padding": ""
            }
            self.receptors.append(r)
        self.refresh_table()

    def remove_receptor(self):
        sel_paths = self.table.selection()
        self.receptors = [r for r in self.receptors if r["path"] not in sel_paths]
        self.refresh_table()

    def move_up(self):
        sel = self.table.selection()
        for s in sel:
            i = next((idx for idx, r in enumerate(self.receptors) if r["path"] == s), None)
            if i is not None and i > 0:
                self.receptors[i], self.receptors[i - 1] = self.receptors[i - 1], self.receptors[i]
        self.refresh_table()
        for s in sel:
            self.table.selection_add(s)

    def move_down(self):
        sel = self.table.selection()
        for s in reversed(sel):
            i = next((idx for idx, r in enumerate(self.receptors) if r["path"] == s), None)
            if i is not None and i < len(self.receptors) - 1:
                self.receptors[i], self.receptors[i + 1] = self.receptors[i + 1], self.receptors[i]
        self.refresh_table()
        for s in sel:
            self.table.selection_add(s)

    def edit_row(self, event):
        if self.run_in_progress:
            return

        sel = self.table.selection()
        if not sel:
            return
        r = next(x for x in self.receptors if x["path"] == sel[0])

        win = tk.Toplevel(self.root)
        win.title(f"Edit {r['name']}")
        mode = tk.StringVar(value=r["mode"])
        res = tk.StringVar(value=r["residues"])
        pad = tk.StringVar(value=r["padding"])

        ttk.Combobox(win, textvariable=mode, values=["Whole", "Active"]).pack(pady=2)
        tk.Entry(win, textvariable=res, width=40).pack(pady=2)
        tk.Entry(win, textvariable=pad, width=10).pack(pady=2)
        tk.Label(win, text="Residues: comma-separated | Padding optional").pack()

        def save():
            r["mode"] = mode.get()
            r["residues"] = res.get()
            r["padding"] = pad.get()
            self.refresh_table()
            self.save_settings()
            win.destroy()

        tk.Button(win, text="Save", command=save).pack(pady=5)

    def refresh_table(self):
        self.table.delete(*self.table.get_children())
        for r in self.receptors:
            pad = r["padding"] if r["padding"] else self.padding_var.get()
            self.table.insert("", "end", iid=r["path"], values=(r["name"], r["mode"], r["residues"], pad))

    # ----------------------
    # Preview
    # ----------------------
    def preview(self):
        if self.run_in_progress:
            return

        sel = self.table.selection()
        if not sel:
            return
        r = next(x for x in self.receptors if x["path"] == sel[0])

        try:
            self.root.config(cursor="watch")
            self.root.update_idletasks()

            atoms = parse_atoms(r["path"])
            if not atoms:
                messagebox.showerror("Error", f"No atoms parsed from:\n{r['path']}")
                return

            padding = float(r["padding"]) if r["padding"] else float(self.padding_var.get())

            box_atoms = atoms
            resset = set()
            if r["mode"] == "Active" and r["residues"]:
                try:
                    resset = {int(x.strip()) for x in r["residues"].split(",") if x.strip()}
                except ValueError:
                    messagebox.showerror("Error", "Residues must be comma-separated integers.")
                    return
                box_atoms = [a for a in atoms if a[3] in resset]
                if not box_atoms:
                    messagebox.showwarning("Warning", "Active residues matched 0 atoms. Falling back to Whole.")
                    box_atoms = atoms

            cx, cy, cz, sx, sy, sz, *_ = box_from_atoms(box_atoms, padding)

            fig = plt.figure()
            ax = fig.add_subplot(111, projection="3d")
            ax.scatter([a[0] for a in atoms], [a[1] for a in atoms], [a[2] for a in atoms], s=1)

            if r["mode"] == "Active" and resset:
                act = [a for a in atoms if a[3] in resset]
                ax.scatter([a[0] for a in act], [a[1] for a in act], [a[2] for a in act], c="red", s=6)

            ax.scatter([cx], [cy], [cz], c="green", s=60)

            rx = [cx - sx / 2, cx + sx / 2]
            ry = [cy - sy / 2, cy + sy / 2]
            rz = [cz - sz / 2, cz + sz / 2]
            for s, e in combinations(list(product(rx, ry, rz)), 2):
                if sum(abs(s[i] - e[i]) for i in range(3)) in (sx, sy, sz):
                    ax.plot3D(*zip(s, e), c="blue")

            plt.show()
        finally:
            try:
                self.root.config(cursor="")
            except Exception:
                pass

    # ----------------------
    # UI state / process control
    # ----------------------
    def set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"

        for child in self.root.winfo_children():
            self._set_state_recursive(child, state)

        if self.overlay is not None and not enabled:
            try:
                self.overlay.pause_btn.config(state="normal")
                self.overlay.stop_after_receptor_btn.config(
                    state="disabled" if self.stop_after_receptor_event.is_set() else "normal"
                )
                self.overlay.cancel_btn.config(state="normal")
            except Exception:
                pass

    def _set_state_recursive(self, widget, state):
        if self.overlay is not None and widget == self.overlay.win:
            return
        try:
            widget.configure(state=state)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._set_state_recursive(child, state)

    def set_current_process(self, proc):
        with self.current_process_lock:
            self.current_process = proc

    def clear_current_process(self, proc):
        with self.current_process_lock:
            if self.current_process is proc:
                self.current_process = None

    def terminate_current_process(self):
        with self.current_process_lock:
            proc = self.current_process

        if proc is None:
            return

        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def on_pause_resume_clicked(self):
        if not self.run_in_progress:
            return

        if self.pause_event.is_set():
            self.pause_event.clear()
            if self.overlay is not None:
                self.overlay.set_paused(False)
                try:
                    current = self.overlay.pb["value"]
                    total = self.overlay.pb["maximum"]
                except Exception:
                    current, total = 0, 1
                self.overlay.update_progress(
                    current, total,
                    status="Resuming...",
                    ligand=self.overlay.ligand_var.get().replace("Ligand: ", "", 1),
                    receptor=self.overlay.receptor_var.get().replace("Receptor: ", "", 1),
                    run_text=self.overlay.run_var.get().replace("Run: ", "", 1)
                )
        else:
            self.pause_event.set()
            if self.overlay is not None:
                self.overlay.set_paused(True)

    def on_stop_after_receptor_clicked(self):
        if not self.run_in_progress:
            return
        if self.stop_after_receptor_event.is_set():
            return

        self.stop_after_receptor_event.set()

        if self.overlay is not None:
            self.overlay.set_stop_after_receptor_requested(True)
            try:
                current = self.overlay.pb["value"]
                total = self.overlay.pb["maximum"]
            except Exception:
                current, total = 0, 1
            self.overlay.update_progress(
                current,
                total,
                status="Stop requested - will stop after current receptor finishes",
                ligand=self.overlay.ligand_var.get().replace("Ligand: ", "", 1),
                receptor=self.overlay.receptor_var.get().replace("Receptor: ", "", 1),
                run_text=self.overlay.run_var.get().replace("Run: ", "", 1)
            )

    def on_cancel_clicked(self):
        if not self.run_in_progress:
            return

        self.cancel_event.set()
        self.pause_event.clear()

        if self.overlay is not None:
            self.overlay.set_paused(False)
            self.overlay.set_cancelled_state()
            try:
                current = self.overlay.pb["value"]
                total = self.overlay.pb["maximum"]
            except Exception:
                current, total = 0, 1
            self.overlay.update_progress(
                current, total,
                status="Cancelling... waiting for current process to stop",
                ligand=self.overlay.ligand_var.get().replace("Ligand: ", "", 1),
                receptor=self.overlay.receptor_var.get().replace("Receptor: ", "", 1),
                run_text=self.overlay.run_var.get().replace("Run: ", "", 1)
            )

        self.terminate_current_process()

    # ----------------------
    # Queue / status pump
    # ----------------------
    def post_progress(self, current, total, status="", ligand="", receptor="", run_text=""):
        self.job_queue.put({
            "type": "progress",
            "current": current,
            "total": total,
            "status": status,
            "ligand": ligand,
            "receptor": receptor,
            "run_text": run_text,
        })

    def post_done(self, kind, title, message):
        self.job_queue.put({
            "type": "done",
            "kind": kind,
            "title": title,
            "message": message,
        })

    def start_progress_poll(self):
        self.root.after(80, self.process_job_queue)

    def process_job_queue(self):
        try:
            while True:
                item = self.job_queue.get_nowait()
                typ = item.get("type")

                if typ == "progress":
                    if self.overlay is not None:
                        self.overlay.update_progress(
                            item.get("current", 0),
                            item.get("total", 1),
                            status=item.get("status", ""),
                            ligand=item.get("ligand", ""),
                            receptor=item.get("receptor", ""),
                            run_text=item.get("run_text", "")
                        )
                        try:
                            self.overlay.win.update_idletasks()
                        except Exception:
                            pass

                elif typ == "done":
                    self.finish_run_ui(item["kind"], item["title"], item["message"])
                    return

        except queue.Empty:
            pass

        if self.run_in_progress:
            self.root.after(80, self.process_job_queue)

    def finish_run_ui(self, kind, title, message):
        self.run_in_progress = False
        self.pause_event.clear()
        self.cancel_event.clear()
        self.stop_after_receptor_event.clear()

        with self.current_process_lock:
            self.current_process = None

        try:
            self.root.config(cursor="")
        except Exception:
            pass

        self.set_controls_enabled(True)

        if self.overlay is not None:
            self.overlay.close()
            self.overlay = None

        if kind == "error":
            messagebox.showerror(title, message)
        else:
            messagebox.showinfo(title, message)

    # ----------------------
    # Pause / cancel / stop helpers for worker
    # ----------------------
    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise RunCancelled("Cancelled by user.")

    def check_stop_after_receptor_boundary(self, runlog, ligand_name, receptor_name):
        if self.stop_after_receptor_event.is_set():
            log_line(
                runlog,
                f"[INFO] Stop-after-receptor triggered after ligand '{ligand_name}', receptor '{receptor_name}'"
            )
            raise RunStoppedAfterReceptor(
                f"Stopped after receptor: ligand '{ligand_name}', receptor '{receptor_name}'"
            )

    def wait_if_paused_or_cancelled(self, current, total, status, ligand, receptor, run_text):
        self.check_cancelled()

        posted = False
        while self.pause_event.is_set():
            self.check_cancelled()
            if not posted:
                self.post_progress(
                    current, total,
                    status="Paused - waiting to resume",
                    ligand=ligand,
                    receptor=receptor,
                    run_text=run_text
                )
                posted = True
            time.sleep(0.15)

        self.check_cancelled()

    def run_tracked_subprocess(self, cmd):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.set_current_process(proc)
        try:
            stdout, stderr = proc.communicate()
        finally:
            self.clear_current_process(proc)

        if self.cancel_event.is_set():
            raise RunCancelled("Cancelled by user.")

        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)

        return stdout, stderr

    # ----------------------
    # PLIP summary filtering
    # ----------------------
    def extract_unl_section(self, text):
        lines = text.splitlines()
        blocks = []
        current = []
        current_header = None

        for line in lines:
            stripped = line.strip()

            is_header = False
            if stripped and ":" in stripped and "(" in stripped and ") -" in stripped:
                is_header = True

            if is_header:
                if current:
                    blocks.append((current_header, "\n".join(current).rstrip() + "\n"))
                current_header = stripped
                current = [line]
            else:
                if current:
                    current.append(line)

        if current:
            blocks.append((current_header, "\n".join(current).rstrip() + "\n"))

        for header, block in blocks:
            if header and header.startswith("UNL:Z:1"):
                return block

        for header, block in blocks:
            if header and header.startswith("UNL:"):
                return block

        return ""

    def rebuild_all_pose_summary(self, plip_dir, runlog, ligand_name, receptor_name):
        summary_files = []
        for fname in sorted(os.listdir(plip_dir)):
            if fname.lower().startswith("pose") and fname.lower().endswith("_unl_only_summary.txt"):
                summary_files.append(os.path.join(plip_dir, fname))

        out_path = os.path.join(plip_dir, "ALL_POSES_UNL_SUMMARY.txt")

        try:
            with open(out_path, "w", encoding="utf-8") as out:
                if not summary_files:
                    out.write("No pose UNL summaries available.\n")
                else:
                    for i, path in enumerate(summary_files):
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            text = f.read().strip()

                        out.write(text + "\n")

                        if i != len(summary_files) - 1:
                            out.write("\n")

            log_line(runlog, f"[INFO] Wrote combined PLIP summary: {out_path}")
            return out_path
        except Exception as e:
            log_line(runlog, f"[ERROR] Failed writing combined PLIP summary: {e}")
            return None

    # ----------------------
    # Internal PLIP runner
    # ----------------------
    def run_plip_for_pdb(self, pdb_file, run_dir, runlog, pose_name, ligand_name, receptor_name):
        self.check_cancelled()

        plip_dir = os.path.join(run_dir, "plip")
        os.makedirs(plip_dir, exist_ok=True)

        temp_pose_dir = tempfile.mkdtemp(prefix=f"{pose_name}_", dir=plip_dir)

        cmd = launchable_program() + [
            "--plip-worker",
            "-f", pdb_file,
            "-o", temp_pose_dir,
            "-t",
            "-x",
            "-v"
        ]

        log_line(runlog, "[PLIP CMD] " + " ".join(cmd))

        try:
            stdout, stderr = self.run_tracked_subprocess(cmd)

            if stdout:
                log_line(runlog, "[PLIP STDOUT] " + stdout.strip().replace("\n", "\n[PLIP STDOUT] "))
            if stderr:
                log_line(runlog, "[PLIP STDERR] " + stderr.strip().replace("\n", "\n[PLIP STDERR] "))

            produced_files = []
            for root, _, files in os.walk(temp_pose_dir):
                for fname in files:
                    src = os.path.join(root, fname)
                    prefixed_name = f"{pose_name}_{fname}"
                    dst = os.path.join(plip_dir, prefixed_name)
                    shutil.move(src, dst)
                    produced_files.append(dst)

            try:
                shutil.rmtree(temp_pose_dir, ignore_errors=True)
            except Exception:
                pass

            report_candidates = []
            for path in produced_files:
                base = os.path.basename(path).lower()
                if base.startswith(f"{pose_name.lower()}_") and (base.endswith(".txt") or base.endswith(".report")):
                    report_candidates.append(path)

            report_candidates.sort(
                key=lambda p: (
                    0 if os.path.basename(p).lower() == f"{pose_name.lower()}_report.txt" else 1,
                    len(p)
                )
            )

            if report_candidates:
                try:
                    with open(report_candidates[0], "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()

                    unl_block = self.extract_unl_section(text)

                    if unl_block.strip():
                        summary_path = os.path.join(plip_dir, f"{pose_name}_UNL_only_summary.txt")
                        with open(summary_path, "w", encoding="utf-8") as f:
                            f.write("=" * 80 + "\n")
                            f.write(f"Ligand: {ligand_name}\n")
                            f.write(f"Receptor: {receptor_name}\n")
                            f.write(f"Pose: {pose_name}\n")
                            f.write("=" * 80 + "\n")
                            f.write(unl_block.strip() + "\n")

                        log_line(runlog, f"[INFO] Wrote UNL-only PLIP summary: {summary_path}")
                    else:
                        log_line(runlog, f"[WARN] Could not find UNL section in PLIP report for {pose_name}.")
                except Exception as e:
                    log_line(runlog, f"[ERROR] Failed creating UNL-only summary for {pose_name}: {e}")
            else:
                log_line(runlog, f"[WARN] No PLIP text report found after rename for {pose_name}.")

        except RunCancelled:
            log_line(runlog, f"[INFO] PLIP cancelled for {pdb_file}")
            raise
        except subprocess.CalledProcessError as e:
            errlog = os.path.join(plip_dir, f"{pose_name}_plip_error.log")
            with open(errlog, "w", encoding="utf-8", errors="replace") as f:
                f.write("CMD:\n" + " ".join(e.cmd) + "\n\n")
                f.write("STDOUT:\n" + (e.output or "") + "\n\n")
                f.write("STDERR:\n" + (e.stderr or "") + "\n")
            log_line(runlog, f"[ERROR] PLIP failed for {pdb_file}. See: {errlog}")
        except Exception as e:
            errlog = os.path.join(plip_dir, f"{pose_name}_plip_error.log")
            with open(errlog, "w", encoding="utf-8", errors="replace") as f:
                f.write("Unexpected PLIP launch error:\n")
                f.write(str(e) + "\n\n")
                f.write(traceback.format_exc())
            log_line(runlog, f"[ERROR] PLIP launch crashed for {pdb_file}. See: {errlog}")
        finally:
            try:
                shutil.rmtree(temp_pose_dir, ignore_errors=True)
            except Exception:
                pass

    # ----------------------
    # Run validation and start
    # ----------------------
    def run_docking(self):
        if self.run_in_progress:
            return

        self.save_settings()

        vina = self.resolved_vina_path()
        outdir = self.outdir_var.get().strip()

        ligands_to_run = list(self.ligands) if self.ligands else []
        if not ligands_to_run:
            single_ligand = self.ligand_var.get().strip()
            if single_ligand:
                ligands_to_run = [single_ligand]

        if not vina or not outdir:
            messagebox.showerror("Error", "Missing Vina or output folder.")
            return

        if not ligands_to_run:
            messagebox.showerror("Error", "No ligand selected. Pick one ligand or add ligands to the queue.")
            return

        if not self.receptors:
            messagebox.showerror("Error", "No receptors loaded.")
            return

        if not os.path.exists(vina):
            messagebox.showerror("Error", f"Vina not found:\n{vina}")
            return

        bad_ligands = [lig for lig in ligands_to_run if not os.path.exists(lig)]
        if bad_ligands:
            messagebox.showerror(
                "Error",
                "These ligand files are missing:\n\n" + "\n".join(bad_ligands[:20])
            )
            return

        try:
            repeats = int(self.repeat_var.get())
            if repeats < 1:
                raise ValueError
        except Exception:
            messagebox.showerror("Error", "Repeats must be a positive integer.")
            return

        try:
            exhaustiveness = str(float(self.exhaust_var.get())).rstrip("0").rstrip(".")
        except Exception:
            messagebox.showerror("Error", "Exhaustiveness must be numeric.")
            return

        try:
            default_padding = float(self.padding_var.get())
        except Exception:
            messagebox.showerror("Error", "Default Padding must be numeric.")
            return

        for ligand in ligands_to_run:
            ligand_ok, ligand_msg = validate_pdbqt(ligand, kind="ligand")
            if not ligand_ok:
                messagebox.showerror("Error", f"Ligand PDBQT sanity check failed:\n{ligand_msg}\n\n{ligand}")
                return

        total_jobs = len(ligands_to_run) * len(self.receptors) * repeats

        self.run_in_progress = True
        self.pause_event.clear()
        self.cancel_event.clear()
        self.stop_after_receptor_event.clear()

        receptors_copy = [dict(r) for r in self.receptors]

        args = {
            "vina": vina,
            "outdir": outdir,
            "ligands_to_run": ligands_to_run,
            "receptors": receptors_copy,
            "repeats": repeats,
            "total_jobs": total_jobs,
            "exhaustiveness": exhaustiveness,
            "default_padding": default_padding,
            "save_merge": bool(self.merge_var.get()),
            "save_pdb": bool(self.pdb_var.get()),
            "save_txt": bool(self.txt_var.get()),
            "run_plip": bool(self.plip_var.get()),
        }

        self.set_controls_enabled(False)

        try:
            self.root.config(cursor="watch")
        except Exception:
            pass

        self.overlay = ProgressOverlay(
            self.root,
            pause_callback=self.on_pause_resume_clicked,
            cancel_callback=self.on_cancel_clicked,
            stop_after_receptor_callback=self.on_stop_after_receptor_clicked
        )
        self.overlay.set_paused(False)
        self.overlay.set_stop_after_receptor_requested(False)
        self.overlay.update_progress(
            0,
            total_jobs,
            status="Preparing run...",
            ligand=f"{len(ligands_to_run)} queued",
            receptor=f"{len(receptors_copy)} receptors loaded",
            run_text=f"Repeats: {repeats}"
        )
        self.root.update_idletasks()

        self.worker_thread = threading.Thread(
            target=self._run_docking_worker,
            args=(args,),
            daemon=True
        )
        self.worker_thread.start()
        self.start_progress_poll()

    # ----------------------
    # Worker helpers
    # ----------------------
    def _run_docking_worker(self, args):
        completed_jobs = 0
        completed_ligands = 0
        global_plip_entries = []
        last_runlog = None

        try:
            for ligand in args["ligands_to_run"]:
                last_runlog, completed_jobs = self._process_single_ligand(
                    args=args,
                    ligand=ligand,
                    completed_jobs=completed_jobs,
                    global_plip_entries=global_plip_entries
                )
                completed_ligands += 1

            if args["run_plip"]:
                self._write_top_level_plip_summary(
                    outdir=args["outdir"],
                    ligands_to_run=args["ligands_to_run"],
                    global_plip_entries=global_plip_entries
                )

            self.post_progress(
                args["total_jobs"],
                args["total_jobs"],
                status="Finished",
                ligand=f"{completed_ligands}/{len(args['ligands_to_run'])} ligands processed",
                receptor="Done",
                run_text="Complete"
            )

            self.post_done(
                "info",
                "Done",
                f"Docking finished.\n\nLigands processed: {completed_ligands}/{len(args['ligands_to_run'])}\nOutput folder:\n{args['outdir']}"
            )

        except RunStoppedAfterReceptor as e:
            try:
                if last_runlog:
                    log_line(last_runlog, f"[INFO] Graceful stop requested: {e}")
            except Exception:
                pass

            self.post_done(
                "info",
                "Stopped",
                f"Run stopped cleanly after current receptor.\n\n{e}"
            )

        except RunCancelled:
            try:
                if last_runlog:
                    log_line(last_runlog, f"[INFO] Run cancelled by user at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception:
                pass

            self.post_done("info", "Cancelled", "Docking run cancelled.")

        except Exception:
            try:
                if last_runlog:
                    log_line(last_runlog, "[FATAL] Unhandled exception:\n" + traceback.format_exc())
            except Exception:
                pass

            self.post_done("error", "Docking failed", traceback.format_exc())

    def _process_single_ligand(self, args, ligand, completed_jobs, global_plip_entries):
        ligand_name = os.path.splitext(os.path.basename(ligand))[0]
        ligand_folder = os.path.join(args["outdir"], ligand_name)
        os.makedirs(ligand_folder, exist_ok=True)

        runlog = os.path.join(ligand_folder, "runlog.txt")

        log_line(runlog, f"=== START {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        log_line(runlog, f"Python: {sys.version}")
        log_line(runlog, f"Vina: {args['vina']}")
        log_line(runlog, f"Ligand: {ligand}")
        log_line(runlog, f"Total queued ligands: {len(args['ligands_to_run'])}")
        log_line(runlog, f"Total receptor x repeat jobs for this ligand: {len(args['receptors']) * args['repeats']}")
        log_line(runlog, f"Grand total jobs this run: {args['total_jobs']}")

        w = warn_path_len("ligand_folder", ligand_folder)
        if w:
            log_line(runlog, w)

        final_summary_lines = ["Receptor\tRun\tAffinity (kcal/mol)\tOutput File\n"]

        for receptor in args["receptors"]:
            completed_jobs = self._process_single_receptor(
                args=args,
                ligand=ligand,
                ligand_name=ligand_name,
                ligand_folder=ligand_folder,
                receptor=receptor,
                runlog=runlog,
                final_summary_lines=final_summary_lines,
                completed_jobs=completed_jobs,
                global_plip_entries=global_plip_entries
            )

            receptor_name = os.path.splitext(receptor["name"])[0]
            self.check_stop_after_receptor_boundary(runlog, ligand_name, receptor_name)

        final_summary_file = os.path.join(ligand_folder, "all_runs_summary.txt")
        with open(final_summary_file, "w", encoding="utf-8") as f:
            f.writelines(final_summary_lines)

        log_line(runlog, f"=== END {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        return runlog, completed_jobs

    def _prepare_receptor_context(self, receptor, default_padding, runlog):
        receptor_name = os.path.splitext(receptor["name"])[0]

        if not os.path.exists(receptor["path"]):
            log_line(runlog, f"[ERROR] Receptor missing: {receptor['path']}")
            return False, {"reason": "missing receptor", "receptor_name": receptor_name}

        rec_ok, rec_msg = validate_pdbqt(receptor["path"], kind=f"receptor {receptor_name}")
        if not rec_ok:
            log_line(runlog, f"[ERROR] Receptor sanity check failed: {rec_msg} -> {receptor['path']}")
            return False, {"reason": "invalid receptor", "receptor_name": receptor_name}

        atoms = parse_atoms(receptor["path"])
        if not atoms:
            log_line(runlog, f"[ERROR] No atoms parsed from receptor: {receptor['path']}")
            return False, {"reason": "no atoms", "receptor_name": receptor_name}

        try:
            padding = float(receptor["padding"]) if receptor["padding"] else default_padding
        except Exception:
            log_line(runlog, f"[ERROR] Invalid padding for {receptor_name}: {receptor['padding']}")
            return False, {"reason": "bad padding", "receptor_name": receptor_name}

        box_atoms = atoms
        if receptor["mode"] == "Active" and receptor["residues"]:
            try:
                resset = {int(x.strip()) for x in receptor["residues"].split(",") if x.strip()}
            except ValueError:
                log_line(runlog, f"[ERROR] Bad residue list for {receptor_name}: {receptor['residues']}")
                return False, {"reason": "bad residue list", "receptor_name": receptor_name}

            box_atoms = [a for a in atoms if a[3] in resset]
            if not box_atoms:
                log_line(runlog, f"[WARN] Active residues matched 0 atoms for {receptor_name}. Falling back to Whole.")
                box_atoms = atoms

        try:
            cx, cy, cz, sx, sy, sz, *_ = box_from_atoms(box_atoms, padding)
        except Exception as ex:
            log_line(runlog, f"[ERROR] Failed to build docking box for {receptor_name}: {ex}")
            return False, {"reason": "bad docking box", "receptor_name": receptor_name}

        return True, {
            "receptor_name": receptor_name,
            "atoms": atoms,
            "padding": padding,
            "center": (cx, cy, cz),
            "size": (sx, sy, sz),
        }

    def _process_single_receptor(self, args, ligand, ligand_name, ligand_folder,
                                 receptor, runlog, final_summary_lines,
                                 completed_jobs, global_plip_entries):
        receptor_basename = os.path.basename(receptor["path"])

        self.wait_if_paused_or_cancelled(
            completed_jobs, args["total_jobs"],
            "Waiting to start receptor...",
            os.path.basename(ligand),
            receptor_basename,
            "-"
        )

        ok, ctx = self._prepare_receptor_context(receptor, args["default_padding"], runlog)
        if not ok:
            completed_jobs += args["repeats"]
            self.post_progress(
                completed_jobs,
                args["total_jobs"],
                status=f"Skipping {ctx['reason']}...",
                ligand=os.path.basename(ligand),
                receptor=receptor_basename,
                run_text="-"
            )
            return completed_jobs

        receptor_name = ctx["receptor_name"]

        base = os.path.join(ligand_folder, receptor_name)
        os.makedirs(base, exist_ok=True)

        summary_folder = os.path.join(base, "summary")
        os.makedirs(summary_folder, exist_ok=True)

        for i in range(args["repeats"]):
            completed_jobs = self._run_single_receptor_job(
                args=args,
                ligand=ligand,
                ligand_name=ligand_name,
                receptor=receptor,
                receptor_name=receptor_name,
                base=base,
                summary_folder=summary_folder,
                runlog=runlog,
                final_summary_lines=final_summary_lines,
                completed_jobs=completed_jobs,
                run_index=i + 1,
                center=ctx["center"],
                size=ctx["size"],
                global_plip_entries=global_plip_entries
            )

        return completed_jobs

    def _run_single_receptor_job(self, args, ligand, ligand_name, receptor, receptor_name,
                                 base, summary_folder, runlog, final_summary_lines,
                                 completed_jobs, run_index, center, size,
                                 global_plip_entries):
        receptor_basename = os.path.basename(receptor["path"])

        self.wait_if_paused_or_cancelled(
            completed_jobs, args["total_jobs"],
            "Waiting to start run...",
            os.path.basename(ligand),
            receptor_basename,
            f"{run_index}/{args['repeats']}"
        )

        self.post_progress(
            completed_jobs,
            args["total_jobs"],
            status="Running docking...",
            ligand=os.path.basename(ligand),
            receptor=receptor_basename,
            run_text=f"{run_index}/{args['repeats']}"
        )

        run_dir = os.path.join(base, f"run_{run_index}")
        dock_dir = os.path.join(run_dir, "dock")
        pose_dir = os.path.join(run_dir, "poses")
        merged_dir = os.path.join(run_dir, "merged")
        pdb_dir = os.path.join(run_dir, "pdb")

        for d in (dock_dir, pose_dir, merged_dir, pdb_dir):
            os.makedirs(d, exist_ok=True)

        dock_out = os.path.join(dock_dir, f"{ligand_name}_{receptor_name}_run{run_index}.pdbqt")

        for label, pth in [("receptor", receptor["path"]), ("ligand", ligand), ("dock_out", dock_out)]:
            w = warn_path_len(label, pth)
            if w:
                log_line(runlog, w)

        cx, cy, cz = center
        sx, sy, sz = size

        cmd = [
            args["vina"],
            "--receptor", receptor["path"],
            "--ligand", ligand,
            "--center_x", str(cx),
            "--center_y", str(cy),
            "--center_z", str(cz),
            "--size_x", str(sx),
            "--size_y", str(sy),
            "--size_z", str(sz),
            "--exhaustiveness", args["exhaustiveness"],
            "--out", dock_out
        ]
        log_line(runlog, f"\n[RUN] {receptor_name} run {run_index}")
        log_line(runlog, "CMD: " + " ".join(cmd))

        try:
            stdout, stderr = self.run_tracked_subprocess(cmd)
            if stdout:
                log_line(runlog, "[VINA STDOUT] " + stdout.strip().replace("\n", "\n[VINA STDOUT] "))
            if stderr:
                log_line(runlog, "[VINA STDERR] " + stderr.strip().replace("\n", "\n[VINA STDERR] "))
        except RunCancelled:
            log_line(runlog, f"[INFO] Cancelled during Vina run for {receptor_name} run {run_index}")
            raise
        except subprocess.CalledProcessError as e:
            errlog = os.path.join(summary_folder, f"vina_error_{receptor_name}_run{run_index}.log")
            with open(errlog, "w", encoding="utf-8", errors="replace") as f:
                f.write("CMD:\n" + " ".join(e.cmd) + "\n\n")
                f.write("STDOUT:\n" + (e.output or "") + "\n\n")
                f.write("STDERR:\n" + (e.stderr or "") + "\n")
            log_line(runlog, f"[ERROR] Vina failed for {receptor_name} run {run_index}. See: {errlog}")

            completed_jobs += 1
            self.post_progress(
                completed_jobs,
                args["total_jobs"],
                status="Run failed, continuing...",
                ligand=os.path.basename(ligand),
                receptor=receptor_basename,
                run_text=f"{run_index}/{args['repeats']}"
            )
            return completed_jobs

        if not wait_for_stable_file(dock_out, timeout_s=30.0, stable_window_s=0.5, cancel_event=self.cancel_event):
            log_line(runlog, f"[ERROR] Docking output not stable/found for {receptor_name} run {run_index}: {dock_out}")
            completed_jobs += 1
            self.post_progress(
                completed_jobs,
                args["total_jobs"],
                status="Output missing/unstable, continuing...",
                ligand=os.path.basename(ligand),
                receptor=receptor_basename,
                run_text=f"{run_index}/{args['repeats']}"
            )
            return completed_jobs

        out_ok, out_msg = validate_pdbqt(dock_out, kind=f"dock_out {receptor_name} run {run_index}")
        if not out_ok:
            log_line(runlog, f"[ERROR] Output sanity check failed: {out_msg} -> {dock_out}")
            completed_jobs += 1
            self.post_progress(
                completed_jobs,
                args["total_jobs"],
                status="Bad output file, continuing...",
                ligand=os.path.basename(ligand),
                receptor=receptor_basename,
                run_text=f"{run_index}/{args['repeats']}"
            )
            return completed_jobs

        self.split_merge_convert(
            dock_pdbqt=dock_out,
            receptor_pdbqt=receptor["path"],
            ligand_name=ligand_name,
            receptor_name=receptor_name,
            run=run_index,
            pose_dir=pose_dir,
            merged_dir=merged_dir,
            pdb_dir=pdb_dir,
            summary_folder=summary_folder,
            final_summary_lines=final_summary_lines,
            run_dir=run_dir,
            runlog=runlog,
            save_merge=args["save_merge"],
            save_pdb=args["save_pdb"],
            save_txt=args["save_txt"],
            run_plip=args["run_plip"],
            global_plip_entries=global_plip_entries
        )

        completed_jobs += 1
        self.post_progress(
            completed_jobs,
            args["total_jobs"],
            status="Completed run",
            ligand=os.path.basename(ligand),
            receptor=receptor_basename,
            run_text=f"{run_index}/{args['repeats']}"
        )

        return completed_jobs

    def _write_top_level_plip_summary(self, outdir, ligands_to_run, global_plip_entries):
        top_plip_file = os.path.join(outdir, "all ligands plip.txt")
        try:
            with open(top_plip_file, "w", encoding="utf-8") as out:
                if not global_plip_entries:
                    out.write("No PLIP summaries were generated.\n")
                else:
                    for i, entry in enumerate(global_plip_entries, 1):
                        with open(entry["path"], "r", encoding="utf-8", errors="replace") as f:
                            text = f.read().strip()

                        out.write("=" * 100 + "\n")
                        out.write(f"Ligand: {entry['ligand']}\n")
                        out.write(f"Receptor: {entry['receptor']}\n")
                        out.write(f"Run: {entry['run']}\n")
                        out.write(f"Pose: {entry['pose']}\n")
                        out.write("=" * 100 + "\n")
                        out.write(text + "\n")

                        if i != len(global_plip_entries):
                            out.write("\n\n")

            for ligand in ligands_to_run:
                ligand_name = os.path.splitext(os.path.basename(ligand))[0]
                ligand_folder = os.path.join(outdir, ligand_name)
                runlog = os.path.join(ligand_folder, "runlog.txt")
                if os.path.exists(runlog):
                    log_line(runlog, f"[INFO] Wrote top-level PLIP summary: {top_plip_file}")

        except Exception as e:
            for ligand in ligands_to_run:
                ligand_name = os.path.splitext(os.path.basename(ligand))[0]
                ligand_folder = os.path.join(outdir, ligand_name)
                runlog = os.path.join(ligand_folder, "runlog.txt")
                if os.path.exists(runlog):
                    log_line(runlog, f"[ERROR] Failed writing top-level PLIP summary: {e}")

    # ----------------------
    # Split / merge / convert
    # ----------------------
    def split_merge_convert(self, dock_pdbqt, receptor_pdbqt,
                            ligand_name, receptor_name, run,
                            pose_dir, merged_dir, pdb_dir,
                            summary_folder, final_summary_lines,
                            run_dir, runlog,
                            save_merge, save_pdb, save_txt, run_plip,
                            global_plip_entries=None):

        self.check_cancelled()

        best_affinity = None
        output_file = os.path.basename(dock_pdbqt)
        run_summary_file = os.path.join(summary_folder, f"{ligand_name}_{receptor_name}_run{run}_summary.txt")

        if not os.path.exists(dock_pdbqt):
            log_line(runlog, f"[WARN] Docking file missing, skipping: {dock_pdbqt}")
            return

        with open(dock_pdbqt, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if save_txt:
            with open(run_summary_file, "w", encoding="utf-8") as f:
                f.writelines(lines)

        receptor_atom_count = self.count_atom_records(receptor_pdbqt)

        pose = []
        idx = 1
        plip_dir = os.path.join(run_dir, "plip")
        generated_pose_summaries = []

        for l in lines:
            self.check_cancelled()

            if l.startswith("MODEL"):
                pose = []

            elif l.startswith("ENDMDL"):
                pose_file = os.path.join(
                    pose_dir, f"{ligand_name}_{receptor_name}_run{run}_pose{idx}.pdbqt"
                )
                with open(pose_file, "w", encoding="utf-8") as o:
                    o.writelines(pose)

                merged_file = pose_file
                if save_merge:
                    merged_file = os.path.join(
                        merged_dir, f"{ligand_name}_{receptor_name}_run{run}_pose{idx}_merged.pdbqt"
                    )
                    with open(merged_file, "w", encoding="utf-8") as m:
                        with open(receptor_pdbqt, "r", encoding="utf-8", errors="replace") as rfile:
                            for rl in rfile:
                                if rl.startswith(("ATOM", "HETATM")):
                                    m.write(rl)
                        for pl in pose:
                            m.write(pl)

                if save_pdb:
                    pdb_file = os.path.join(
                        pdb_dir, f"{ligand_name}_{receptor_name}_run{run}_pose{idx}.pdb"
                    )
                    self.pdbqt_to_pdb(merged_file, pdb_file, receptor_atom_count)

                    if run_plip:
                        self.check_cancelled()
                        pose_name = f"pose{idx}"
                        self.run_plip_for_pdb(
                            pdb_file, run_dir, runlog, pose_name, ligand_name, receptor_name
                        )

                        pose_summary = os.path.join(plip_dir, f"{pose_name}_UNL_only_summary.txt")
                        if os.path.exists(pose_summary):
                            generated_pose_summaries.append(pose_summary)
                            if global_plip_entries is not None:
                                global_plip_entries.append({
                                    "ligand": ligand_name,
                                    "receptor": receptor_name,
                                    "run": run,
                                    "pose": pose_name,
                                    "path": pose_summary
                                })

                idx += 1

            else:
                if l.startswith("REMARK VINA RESULT:"):
                    try:
                        aff = float(l.split()[3])
                        if best_affinity is None or aff < best_affinity:
                            best_affinity = aff
                    except Exception:
                        pass

                if l.startswith(("ATOM", "HETATM")):
                    pose.append(l)

        if run_plip and save_pdb and os.path.isdir(plip_dir) and generated_pose_summaries:
            self.rebuild_all_pose_summary(plip_dir, runlog, ligand_name, receptor_name)

        if best_affinity is not None:
            final_summary_lines.append(f"{receptor_name}\tRun {run}\t{best_affinity}\t{output_file}\n")

    # ----------------------
    # PDB helpers
    # ----------------------
    def count_atom_records(self, path):
        n = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(("ATOM", "HETATM")):
                    n += 1
        return n

    def guess_element(self, line):
        parts = line.split()
        atomtype = parts[-1].strip() if parts else ""
        at_up = atomtype.upper().replace(".", "")

        autodock_map = {
            "C": "C",
            "A": "C",
            "N": "N",
            "NA": "N",
            "NS": "N",
            "OA": "O",
            "OS": "O",
            "O": "O",
            "S": "S",
            "SA": "S",
            "P": "P",
            "F": "F",
            "CL": "Cl",
            "BR": "Br",
            "I": "I",
            "HD": "H",
            "H": "H",
            "MG": "Mg",
            "ZN": "Zn",
            "CA": "Ca",
            "MN": "Mn",
            "FE": "Fe",
            "CU": "Cu",
            "K": "K",
            "NA+": "Na",
            "NA1": "Na",
        }

        if at_up in autodock_map:
            return autodock_map[at_up]

        atom_name = line[12:16].strip()
        atom_up = atom_name.upper()

        if atom_up.startswith("CL"):
            return "Cl"
        if atom_up.startswith("BR"):
            return "Br"
        if atom_up.startswith("ZN"):
            return "Zn"
        if atom_up.startswith("FE"):
            return "Fe"
        if atom_up.startswith("MG"):
            return "Mg"
        if atom_up.startswith("CA"):
            return "Ca"
        if atom_up.startswith("MN"):
            return "Mn"
        if atom_up.startswith("CU"):
            return "Cu"

        if atom_up:
            ch = atom_up[0]
            if ch in {"H", "C", "N", "O", "S", "P", "F", "I", "K", "B"}:
                return ch

        return "C"

    def is_ligand_hydrogen(self, line):
        element = self.guess_element(line)
        if element == "H":
            return True

        atom_name = line[12:16].strip().upper()
        if atom_name.startswith("H"):
            return True

        return False

    def make_atom_name(self, element, count):
        return f"{element}{count}"

    def format_pdb_atom_line(
        self,
        record,
        serial,
        atom_name,
        resname,
        chain,
        resid,
        x,
        y,
        z,
        occupancy,
        temp_factor,
        element
    ):
        atom_name = (atom_name or "X")[:4]
        resname = (resname or "UNK")[:3]
        chain = (chain or "A")[:1]
        element = (element or "").strip()[:2].rjust(2)

        if len(atom_name) < 4:
            atom_name = atom_name.rjust(4)

        altloc = ""
        icode = ""

        return (
            f"{record:<6}"
            f"{serial:>5} "
            f"{atom_name:<4}"
            f"{altloc:1}"
            f"{resname:>3} "
            f"{chain:1}"
            f"{resid:>4}"
            f"{icode:1}   "
            f"{x:>8.3f}"
            f"{y:>8.3f}"
            f"{z:>8.3f}"
            f"{occupancy:>6.2f}"
            f"{temp_factor:>6.2f}"
            f"          "
            f"{element:>2}\n"
        )

    def pdbqt_to_plip_ready_pdb(self, src, dst, receptor_atom_count):
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.read().splitlines()

        atom_lines = [line for line in raw_lines if line.startswith(("ATOM", "HETATM"))]

        if not atom_lines:
            raise ValueError(f"No ATOM/HETATM records found in {src}")

        protein_lines = atom_lines[:receptor_atom_count]
        ligand_lines = atom_lines[receptor_atom_count:]

        if not protein_lines:
            raise ValueError("No protein atoms found during conversion.")

        if not ligand_lines:
            raise ValueError("No ligand atoms found during conversion.")

        out_lines = []

        for line in protein_lines:
            out_lines.append(line[:66].rstrip() + "\n")

        out_lines.append("TER\n")

        serial = receptor_atom_count + 2
        element_counts = {}

        for line in ligand_lines:
            if self.is_ligand_hydrogen(line):
                continue

            try:
                x = float(line[30:38])
                y = float(line[38:46])
                z = float(line[46:54])
            except Exception:
                continue

            element = self.guess_element(line)
            element_counts[element] = element_counts.get(element, 0) + 1
            atom_name = self.make_atom_name(element, element_counts[element])

            out_lines.append(
                self.format_pdb_atom_line(
                    record="HETATM",
                    serial=serial,
                    atom_name=atom_name,
                    resname="UNL",
                    chain="Z",
                    resid=1,
                    x=x,
                    y=y,
                    z=z,
                    occupancy=1.00,
                    temp_factor=0.00,
                    element=element
                )
            )
            serial += 1

        out_lines.append("END\n")

        with open(dst, "w", encoding="utf-8") as out:
            out.writelines(out_lines)

    def pdbqt_to_pdb(self, src, dst, receptor_atom_count):
        self.pdbqt_to_plip_ready_pdb(src, dst, receptor_atom_count)


# ======================================================
if __name__ == "__main__":
    if "--plip-worker" in sys.argv:
        sys.exit(run_internal_plip_worker())

    root = tk.Tk()
    app = VinaBatchGUI(root)
    root.mainloop()