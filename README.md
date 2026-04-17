# VLIP – Simple Docking Tool (Vina + PLIP)

This is a **one-click tool** to run docking jobs using AutoDock Vina, with optional PLIP analysis.

If you can click buttons and pick files, you can use this.

---

## 🧠 What this does

- Runs docking (Vina)
- Splits poses
- Converts to PDB
- Runs PLIP (optional)
- Saves everything neatly

---

## ⚙️ What you need

### REQUIRED
- `VLIP.exe`

### ALSO REQUIRED (IMPORTANT)
- `vina.exe` (must be in same folder OR installed)

### MAYBE REQUIRED (for PLIP)
- Open Babel installed on your system

If PLIP doesn’t work → this is probably why.

---

## 🚀 HOW TO USE

### 1. Open the program
Double-click:
VLIP.exe

---

### 2. Add receptor(s)
Click:
Add receptor

Pick `.pdbqt` files

---

### 3. Add ligand(s)

You have TWO options:

#### Option A (single ligand)
- Use the “Ligand” box

#### Option B (multiple ligands)
Click:
Add ligand(s)

---

### 4. Choose output folder
Click:
Select Output Folder

---

### 5. (Optional settings)

- Repeats = how many times to run docking
- Exhaustiveness = how hard Vina searches
- PLIP = interaction analysis

If you don’t know → leave defaults

---

### 6. RUN

Click the big green button:
RUN DOCKING

Wait.

---

## 📂 WHERE ARE MY FILES?

Everything goes into your output folder:

output/
 └── ligand_name/
     └── receptor_name/
         └── run_1/
             ├── dock/
             ├── poses/
             ├── merged/
             ├── pdb/
             └── plip/

---

## 📄 IMPORTANT FILES

- all_runs_summary.txt → best scores
- ALL_POSES_UNL_SUMMARY.txt → PLIP interactions
- all ligands plip.txt → everything combined

---

## ⚠️ COMMON PROBLEMS

### Nothing happens
- Vina not found → put `vina.exe` next to the program

---

### PLIP not working
- Install Open Babel

---

### Crashes
- Bad `.pdbqt` file
- Try another file

---

### Weird results
- That’s docking

---

## ⚠️ DISCLAIMER

This is a computational tool.

- Results are predictions
- Not experimentally validated
- Not medical advice
- Not guaranteed to be correct

---

## 💬 FINAL NOTE

If it breaks:
- That’s useful
- Tell me what broke
