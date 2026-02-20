```markdown
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

```

