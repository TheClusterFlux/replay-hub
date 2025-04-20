# new-project-template

once read, please replace this readme with one detailing your service, what it does, how to use it, and any other relevant information. 😊

This repository is a template for creating new projects with Kubernetes-based deployments. It includes tools and configurations to streamline the setup process.

## Key Features

- **Deployment YAML Generator**: A Bash script (`init.sh`) to generate Kubernetes deployment, service, and optional ingress configurations.
- **GitHub Actions Workflow**: Automates the deployment of your service to a Kubernetes cluster.

## How to Use

1. **Clone or Use as a Template**:
   - Clone this repository or use the "Use this template" button on GitHub to create a new repository.

2. **Run the Deployment YAML Generator**:
   - Execute the `init.sh` script to generate Kubernetes YAML files:
     ```bash
     ./init.sh
     ```

3. **Add Your Code**:
   - Add your service code and a `Dockerfile` to the repository.
   - Ensure the service is exposed on port 8080 or update the generated YAML accordingly.

4. **Trigger Deployment**:
   - Push changes to the `main` branch to deploy your service using the pre-configured GitHub Actions workflow.

This template simplifies Kubernetes deployments and ensures consistency across projects.