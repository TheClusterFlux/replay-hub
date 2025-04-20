#!/bin/bash

# Prompt for project-specific information
read -p "Enter the service name (SERVICE_NAME): " SERVICE_NAME

# Infer the image tag from the service name
IMAGE_TAG=$(basename "$(pwd)")

# Ask if an Ingress should be included
read -p "Do you want to include an Ingress? (y/n): (Ingress exposes your service to the internet) " INCLUDE_INGRESS

# If Ingress is included, prompt for the subdomain
if [[ "$INCLUDE_INGRESS" == "y" ]]; then
    read -p "Enter the subdomain for the Ingress (e.g., 'myapp' for myapp.theclusterflux.com): " SUBDOMAIN
fi

# Create the deployment YAML
cat <<EOF > deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $SERVICE_NAME
spec:
  replicas: 1
  selector:
    matchLabels:
      app: $SERVICE_NAME
  template:
    metadata:
      labels:
        app: $SERVICE_NAME
    spec:
      containers:
        - name: $SERVICE_NAME
          image: docker.io/keanuwatts/theclusterflux:$IMAGE_TAG
          imagePullPolicy: Always
      imagePullSecrets:
        - name: dockerhub-secret
---
apiVersion: v1
kind: Service
metadata:
  name: $SERVICE_NAME
spec:
  selector:
    app: $SERVICE_NAME
  ports:
    - protocol: TCP
      port: 8080
      targetPort: 8080
EOF

# Optionally add the Ingress resource
if [[ "$INCLUDE_INGRESS" == "y" ]]; then
  cat <<EOF >> deployment.yaml
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: $SERVICE_NAME
  namespace: default
  annotations:
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - $SUBDOMAIN.theclusterflux.com
    secretName: theclusterflux
  rules:
  - host: $SUBDOMAIN.theclusterflux.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: $SERVICE_NAME
            port:
              number: 8080
EOF
fi


echo "Deployment YAML file 'deployment.yaml' has been created."

# Wait for user input before self-deletion
read -n 1 -s -r -p "Press any key to continue..."

rm -- "$0"