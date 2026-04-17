# VLIP

VLIP is a Windows GUI tool for running batch molecular docking with AutoDock Vina and optional PLIP interaction analysis.

Everything needed to run the packaged build is intended to be included in the build. You should not need to install separate tools for normal use of the packaged version.

## Contact

For bug reports, feedback, questions, or problems, email:

**muguy2205@gmail.com**

## AI use

AI was used during development of this project to help with programming, debugging, restructuring code, and improving the interface and workflow. The tool was still tested, edited, and directed by the author.

## What this program does

VLIP is designed to make docking runs easier to manage through a normal GUI instead of manually running command line tools over and over.

It can:

- run docking with one ligand or many ligands
- run against one receptor or many receptors
- repeat runs multiple times
- automatically build a docking box from either the whole receptor or selected active-site residues
- split docking output into individual poses
- optionally merge receptor and ligand into one file
- optionally convert merged docking output into PDB format
- optionally run PLIP on the generated PDB files
- generate summaries for individual runs and combined summaries for all poses

## Important file types

This program expects **PDBQT** files for docking input.

### Receptors
Receptors must be supplied as **`.pdbqt`** files.

Use the **Add receptor** button to load receptor PDBQT files.

### Ligands
Ligands must also be supplied as **`.pdbqt`** files.

You can either:
- use the single **Ligand** box for one ligand, or
- use the **Ligand Queue** to run many ligand PDBQT files in one batch

If your receptor or ligand is not in PDBQT format, convert it before using VLIP.

## Basic use

### 1. Start the program
Open `VLIP.exe`.

On startup, the program may run a self-check. This is there to help catch problems early.

### 2. Add receptor files
Click **Add receptor** and select one or more receptor `.pdbqt` files.

These will appear in the receptor table.

### 3. Add ligand files
You have two ways to supply ligands:

#### Single ligand
Use the **Ligand** field and browse to one ligand `.pdbqt` file.

#### Multiple ligands
Use the ligand queue buttons:
- **Add current ligand** adds the ligand currently shown in the Ligand field
- **Add ligand(s)** adds one or more ligand `.pdbqt` files directly
- **Remove** removes selected ligands from the queue
- **Move Up / Move Down** changes queue order
- **Clear Queue** removes everything from the queue

If the ligand queue is not empty, VLIP uses the queue.
If the ligand queue is empty, VLIP uses the single Ligand field.

### 4. Choose an output folder
Click **Select Output Folder** and choose where results should be saved.

### 5. Set run options
The main settings are:

- **Default Padding**  
  Extra space added around the atoms used to define the docking box.

- **Repeats**  
  How many times each ligand/receptor combination is run.

- **Exhaustiveness**  
  Vina search effort. Higher values mean a more thorough search but slower runs.

### 6. Optional output options
You can enable or disable these checkboxes:

- **Merge receptor+ligand (merged.pdbqt)**  
  Saves a combined receptor and docked ligand PDBQT file for each pose.

- **Convert to PDB**  
  Converts merged output into PDB format.

- **Save per-run summary (.txt)**  
  Saves the raw docking output text for each run.

- **Run PLIP on PDB output**  
  Runs PLIP on generated PDB files and creates interaction summaries.

### 7. Run docking
Click the big **RUN DOCKING** button.

A progress window will appear showing:
- current ligand
- current receptor
- current run number
- total progress

## Receptor modes

Each receptor can be edited by double-clicking it in the receptor table.

There are two modes:

### Whole
The docking box is built from the whole receptor structure.

Use this if you want broad coverage or do not want to define a specific binding region.

### Active
The docking box is built only from selected residue numbers.

To use this mode:
- set Mode to **Active**
- enter residues as comma-separated integers, for example: `147,151,245,249`

If the listed residues do not match any atoms, VLIP falls back to whole-receptor mode.

## Buttons and what they do

### Top row buttons

**Add receptor**  
Add one or more receptor `.pdbqt` files.

**Remove**  
Remove selected receptor entries.

**Move Up / Move Down**  
Reorder receptors in the receptor list.

**Select Output Folder**  
Choose where all output files will be written.

**Diagnose**  
Runs an internal diagnostic check and writes a diagnostics text file. Use this if the program is not behaving properly.

## Preview

The **Preview** button shows a 3D view of the receptor atoms and the calculated docking box.

This is useful for checking whether your box looks sensible before running the batch.

In preview:
- receptor atoms are shown
- active-site atoms are highlighted if Active mode is used
- the docking box is drawn around the selected region

## During a run

The progress window gives you control over the batch.

### Pause
Pause stops new jobs from starting. It waits for the current subprocess to finish, then pauses.

### Stop After Receptor
This tells VLIP to finish the current receptor and its repeats, then stop cleanly.

This is useful if you want the current receptor finished but do not want the rest of the full batch to continue.

### Cancel Run
This stops the whole run as soon as possible.

## Output structure

Results are saved into the output folder you selected.

The structure will normally look like this:

```text
output_folder/
    ligand_name/
        runlog.txt
        all_runs_summary.txt
        receptor_name/
            summary/
            run_1/
                dock/
                poses/
                merged/
                pdb/
                plip/
            run_2/
                ...
