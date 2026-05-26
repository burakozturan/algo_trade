# How to apply these files to your repo

# 1. Clone or open your local repo
git clone https://github.com/burakozturan/algo_trade.git
cd algo_trade

# 2. Copy these generated files into the repo:
#    README.md
#    .env.example
#    .github/pull_request_template.md
#    docs/resume_project_snippet.md

# 3. Review the diff
git diff

# 4. Commit
git add README.md .env.example .github/pull_request_template.md docs/resume_project_snippet.md
git commit -m "Improve repo documentation for quant research workflows"

# 5. Push
git push
