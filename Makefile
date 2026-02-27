.PHONY: install dev test lint format clean help run run-remote remote-attach remote-stop \
       bump-patch bump-minor bump-major release version

# Default target
help:
	@echo "Available commands:"
	@echo "  install       - Install production dependencies"
	@echo "  dev           - Install development dependencies"
	@echo "  test          - Run tests"
	@echo "  lint          - Run linting checks"
	@echo "  format        - Format code"
	@echo "  clean         - Clean up generated files"
	@echo "  run           - Run the bot"
	@echo "  version       - Show current version"
	@echo "  bump-patch    - Bump patch version (1.2.0 -> 1.2.1), commit, and tag"
	@echo "  bump-minor    - Bump minor version (1.2.0 -> 1.3.0), commit, and tag"
	@echo "  bump-major    - Bump major version (1.2.0 -> 2.0.0), commit, and tag"
	@echo "  release       - Push current version tag to trigger release workflow"
	@echo "  run-remote    - Start bot in tmux on remote Mac (unlocks keychain)"
	@echo "  remote-attach - Attach to running bot tmux session"
	@echo "  remote-stop   - Stop the bot tmux session"

install:
	uv sync --no-dev

dev:
	uv sync
	uv run pre-commit install --install-hooks || echo "pre-commit not configured yet"

test:
	uv run pytest

lint:
	uv run black --check src tests
	uv run isort --check-only src tests
	uv run flake8 src tests
	uv run mypy src

format:
	uv run black src tests
	uv run isort src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

run:
	uv run claude-telegram-bot

# For debugging
run-debug:
	uv run claude-telegram-bot --debug

# Remote Mac Mini (SSH session)
run-remote:  ## Start bot on remote Mac in tmux (persists after SSH disconnect)
	security unlock-keychain ~/Library/Keychains/login.keychain-db
	tmux new-session -d -s claude-bot 'uv run claude-telegram-bot'
	@echo "Bot started in tmux session 'claude-bot'"
	@echo "  Attach: make remote-attach"
	@echo "  Stop:   make remote-stop"

remote-attach:  ## Attach to running bot tmux session
	tmux attach -t claude-bot

remote-stop:  ## Stop the bot tmux session
	tmux kill-session -t claude-bot

# --- Version Management ---

version:  ## Show current version
	@python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"

bump-patch:  ## Bump patch version, commit, and tag
	@python -c "\
	import tomllib, re, pathlib;\
	p = pathlib.Path('pyproject.toml');\
	data = tomllib.loads(p.read_text());\
	parts = data['project']['version'].split('.');\
	parts[2] = str(int(parts[2]) + 1);\
	new_ver = '.'.join(parts);\
	p.write_text(re.sub(r'version = \"[^\"]+\"', f'version = \"{new_ver}\"', p.read_text(), count=1));\
	print(new_ver)" > /tmp/.newver && \
	NEW_VERSION=$$(cat /tmp/.newver) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

bump-minor:  ## Bump minor version, commit, and tag
	@python -c "\
	import tomllib, re, pathlib;\
	p = pathlib.Path('pyproject.toml');\
	data = tomllib.loads(p.read_text());\
	parts = data['project']['version'].split('.');\
	parts[1] = str(int(parts[1]) + 1);\
	parts[2] = '0';\
	new_ver = '.'.join(parts);\
	p.write_text(re.sub(r'version = \"[^\"]+\"', f'version = \"{new_ver}\"', p.read_text(), count=1));\
	print(new_ver)" > /tmp/.newver && \
	NEW_VERSION=$$(cat /tmp/.newver) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

bump-major:  ## Bump major version, commit, and tag
	@python -c "\
	import tomllib, re, pathlib;\
	p = pathlib.Path('pyproject.toml');\
	data = tomllib.loads(p.read_text());\
	parts = data['project']['version'].split('.');\
	parts[0] = str(int(parts[0]) + 1);\
	parts[1] = '0';\
	parts[2] = '0';\
	new_ver = '.'.join(parts);\
	p.write_text(re.sub(r'version = \"[^\"]+\"', f'version = \"{new_ver}\"', p.read_text(), count=1));\
	print(new_ver)" > /tmp/.newver && \
	NEW_VERSION=$$(cat /tmp/.newver) && \
	git add pyproject.toml && \
	git commit -m "release: v$$NEW_VERSION" && \
	git tag "v$$NEW_VERSION" && \
	git push && git push origin "v$$NEW_VERSION" && \
	echo "Released v$$NEW_VERSION. Tag pushed — release workflow will run on GitHub."

release:  ## Push the current version tag to trigger the release workflow
	CURRENT_VERSION=$$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])") && \
	git push && git push origin "v$$CURRENT_VERSION" && \
	echo "Pushed v$$CURRENT_VERSION. Release workflow will run on GitHub."
