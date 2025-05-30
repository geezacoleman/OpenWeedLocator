# Setting Up a Self-Hosted GitHub Runner

The Yocto build workflow can run on a self-hosted runner. Use this approach when you need custom hardware or more control over the build environment.

## 1. Install the Runner

1. Create a directory for the runner and download the latest release:
   ```bash
   mkdir actions-runner && cd actions-runner
   curl -o actions-runner-linux-x64-2.317.0.tar.gz -L https://github.com/actions/runner/releases/download/v2.317.0/actions-runner-linux-x64-2.317.0.tar.gz
   tar xzf actions-runner-linux-x64-2.317.0.tar.gz
   ```
2. Configure the runner, replacing the URL and token with values from your repository settings:
   ```bash
   ./config.sh --url https://github.com/your-user/OpenWeedLocator --token YOUR_TOKEN
   ```
3. Start the runner service:
   ```bash
   ./run.sh
   ```

## 2. Runner Labels

The repository's Yocto build workflow runs on a runner labelled `self-hosted` and `linux`. Ensure your runner includes these labels in the configuration step above. Additional labels such as `x64` are optional but can help target specific hardware.

## 3. Workflow Usage

Once the runner is online, push changes to the repository. GitHub Actions will automatically dispatch the workflow to your self-hosted runner.
