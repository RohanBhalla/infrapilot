# Golden templates (source #58)

Hand-verified, working examples per task type. Each file here is ingested into the
knowledge base as `doc_type=template` and is the first thing generators retrieve.

Add one folder per task, with files that have actually been deployed successfully:

```
golden_templates/
├── cicd_github_actions/node-ci.yml
├── containerize/Dockerfile
├── deploy_lambda/app.py            # CDK app
├── deploy_static_site/app.py       # CDK app
└── eks_cluster/cluster.yaml        # eksctl config
```

Supported file types: .md, .txt, .yml/.yaml, .json, .py, .ts, .js, .guard, and Dockerfiles
saved with a `.txt` suffix or wrapped in a .md file. This README is not ingested.
