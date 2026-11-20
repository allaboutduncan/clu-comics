# Contributing to CLU-Comics

First off, thank you for considering contributing! Whether you are fixing a bug, adding a new metadata provider, or improving the UI, your help makes CLU better for everyone.

To keep things organized and maintainable, please follow these guidelines.

## 🛠️ Getting Started

1. **Fork the repository** and create your branch from `main`.
2. **Install dependencies**: We recommend using a virtual environment.
```bash
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt

```


3. **Run the tests**: `pip install -r requirements-test.txt`, then `pytest -n auto -q`. They need no API keys or network access.
4. **Run the app** (optional): `python app.py`, then open http://localhost:5577. Provider API keys (ComicVine, Metron, etc.) are entered in the app under **Settings**, not in a file.

## 📝 Contribution Workflow

### 1. Open an Issue

Before starting any major work, please open an issue to discuss the change. This helps avoid duplicate work and ensures the feature aligns with the project's goals.

### 2. Branch Naming

Keep it descriptive:

* `feature/add-new-provider`
* `fix/sqlite-locking-issue`
* `docs/update-readme`

### 3. Coding Standards

* **Python:** Follow **PEP 8** style guidelines.
* **Documentation:** If you add a new feature or API integration, update the `README.md` or the `/docs` folder.
* **Docker:** If your change affects the container environment, ensure the `Dockerfile` remains optimized and follows the principle of least privilege.

## 🧪 Testing & Quality Control

We use automated tests to ensure stability. **Pull Requests will not be merged until all checks pass.**

* **Run tests locally:** Before pushing, run your test suite (e.g., `pytest`) to catch errors early.
* **Failed Tests:** If your PR fails the GitHub Actions check, please review the logs, fix the issues locally, and push the updates to your branch.
* **Security:** Do not commit API keys or sensitive configurations. Use environment variables for all secrets.

## 🤝 Pull Request Process

1. Ensure your code is well-commented where logic is complex.
2. Update the `CHANGELOG.md` with a brief summary of your changes.
3. Submit the PR and link it to the relevant issue.
4. Maintainers (or the lead developer) will review your code. Please be prepared to iterate on feedback!

## 🛡️ Security

If you find a security vulnerability, please refer to our [Security Policy](SECURITY.md) and do not report it through a public issue.
