# Industry-Practicum_PartnerLinQ

Hey team, I have created this repo to add our codes and other information so that we can all access and keep a check on the project flow. 

To keep our code organized and prevent anyone from accidentally overwriting someone else's work, we will be using a **Feature Branch Workflow**. Please read the instructions below before making your first code contribution.

---

## 🛠 Phase 1: First-Time Setup (Do this once)

Before you can add code, you need to download a copy of this repository to your local computer.

1. Open your terminal or command prompt.
2. Clone this repository by running:
   `git clone https://github.com/Preetham33/Industry-Practicum_PartnerLinQ.git`
3. Navigate into the new folder:
   `cd Industry-Practicum_PartnerLinQ`

---

## 💻 Phase 2: Daily Workflow (Do this every time you code)

**🚨 CRITICAL RULE: Never write code or push directly to the `main` branch!** Always create a new branch for your specific task (e.g., a new data model, a UI fix, etc.).

Follow these steps exactly whenever you start working:

**1. Get the latest updates from the team**
Make sure your local computer is perfectly synced with the main project before you start.
```bash
git checkout main
git pull origin main

```

**2. Create your own workspace (Branch)**
Create a new branch named after yourself and the task you are doing.

```bash
git checkout -b yourname/task-description
# Example: git checkout -b preetham/data-cleaning-script

```

**3. Write your code & save (Commit)**
Work on your files. When you hit a good stopping point, save your progress to Git.

```bash
git add .
git commit -m "Brief description of what you changed or added"

```

**4. Push your branch to GitHub**
Upload your specific branch to our shared cloud repository.

```bash
git push origin yourname/task-description

```

---

## 🤝 Phase 3: Merging Your Code (Pull Requests)

Once you have pushed your branch to GitHub, you need to ask the team to review and merge it into the `main` project.

1. Go to this repository's homepage on GitHub.
2. You will see a green button that says **"Compare & pull request"** next to your recently pushed branch. Click it!
3. **Leave a comment** explaining what your code does so the rest of the group understands.
4. **Wait for a review:** At least one other teammate should look at your code to make sure it looks good.
5. **Merge:** Once approved, click the **Merge pull request** button on GitHub.
6. **Clean up:** Delete your feature branch on GitHub, go back to your terminal, and run `git checkout main` followed by `git pull origin main` to sync your computer up with your newly merged code!

---

## AI Agent — Setup & Usage

The `modules/` folder contains a **Conversational Mapping Intelligence Agent** that can explain, audit, simulate, modify, and generate EDI/XML/XSLT mappings using an LLM (Groq).

### Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Configure your API key

Copy the example env file and add your Groq API key:

```bash
cp .env.example .env
```

Edit `.env` and set:

```
GROQ_API_KEY=your_groq_api_key_here
```

Get a free key at https://console.groq.com

### Step 3 — Add your mapping files

Copy your XSLT / XML / XSD / EDI mapping files into the `data/` folder (any sub-folder structure is fine):

```
data/
├── 810_Invoice/
│   └── 810_NordStrom_Xslt.xml
├── 850_PO/
│   └── Graybar_850_XSLT.xml
└── your_mapping.xslt
```

### Step 4 — Build the RAG index

Run this once after adding files. Re-run whenever you add new files.

```bash
python scripts/index_data.py
```

To force a full rebuild from scratch:

```bash
python scripts/index_data.py --force
```

### Step 5 — Use the agent

**Single-file question (any of the 5 intents):**

```python
from modules.dispatcher import dispatch

result = dispatch(
    user_message="Explain what this XSLT does.",
    file_path="data/810_Invoice/810_NordStrom_Xslt.xml",
)
print(result["primary_response"])
```

**Audit a mapping for production issues:**

```python
result = dispatch(
    user_message="Audit this mapping and flag any issues before go-live.",
    file_path="data/810_Invoice/810_NordStrom_Xslt.xml",
)
prose = result["primary_response"]
questions = result["audit_dict"]["questions"]   # structured form questions
print(prose)
```

**Cross-file questions across the whole data/ folder (RAG):**

```python
from modules.dispatcher import dispatch_folder

result = dispatch_folder(
    user_message="Which mappings handle 810 invoices?",
    folder_path="data",
)
print(result["primary_response"])
```

**Multi-turn session (all intents share memory):**

```python
from modules.session import Session
from modules.dispatcher import dispatch

session = Session()

r1 = dispatch("Explain this mapping.", file_path="data/810_Invoice/810_NordStrom_Xslt.xml", session=session)
r2 = dispatch("Now audit it.", session=session)          # file remembered from r1
r3 = dispatch("Modify the sender ID to PROD001.", session=session)
```

### Supported file types

| Extension | Format |
|-----------|--------|
| `.xml` / `.xsl` / `.xslt` | Altova MapForce XSLT stylesheets |
| `.xsd` | XML Schema definitions |
| `.edi` / `.txt` | X12 EDI / EDIFACT interchange files |


---

## Running the Web UI

Make sure all dependencies are installed, then launch the Streamlit app:

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open **http://localhost:8501** in your browser.

### UI overview

| Area | What it does |
|------|-------------|
| **Sidebar — Upload Files** | Upload one or more mapping files at any point (`.xml .xsl .xslt .xsd .edi .txt`). All files stay active for the session; remove individual files with the ✕ button. |
| **Sidebar — RAG Index** | Shows how many files are indexed in `data/` and lets you re-index without leaving the app. |
| **Chat** | Single unified chat — all five intents (explain / simulate / modify / generate / audit) work across all uploaded files. The agent picks the most relevant file per turn and auto-injects context from the RAG index. |
| **Audit Form** | Auto-appears after an `audit` intent; fill in the verification questions and submit for a second-pass `SAFE / REVIEW / DO NOT DEPLOY` verdict. |
| **New Session** | Clears conversation history, all uploaded files, and the audit form. |

