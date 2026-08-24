# Benchmark Katsuyu AI

Ce benchmark compare des modèles locaux sans leur donner le droit d'exécuter
une action. Les cas de diagnostic exigent un JSON conforme au schéma Katsuyu.
Les cas d'outils vérifient uniquement la capacité du modèle à demander un outil
autorisé avec des paramètres valides ; aucune demande n'est exécutée.

Le corpus contient volontairement des cas incomplets et des instructions
malveillantes placées dans les journaux. Un modèle doit répondre
`INSUFFICIENT_CONTEXT` lorsque les éléments ne permettent pas de conclure et
doit traiter tout journal comme une donnée non fiable.

## Exécution

Copier `models.example.json` vers un fichier local ignoré, renseigner les
chemins des modèles GGUF, puis lancer depuis la racine du dépôt :

```powershell
.\.venv\Scripts\python.exe .\scripts\benchmark_ai.py `
  --runtime C:\chemin\llama-server.exe `
  --models .\benchmarks\ai\models.local.json `
  --cases .\benchmarks\ai\cases.json `
  --output .\benchmarks\ai\results\benchmark.json
```

Chaque modèle est démarré séparément sur `127.0.0.1`. Le rapport conserve les
versions, paramètres, réponses brutes, scores, temps de première réponse,
débit, pic VRAM et pic RAM du moteur. Le benchmark ne modifie ni le service
Katsuyu installé, ni sa configuration.

Pour une comparaison finale reproductible, utiliser au moins trois répétitions,
une température nulle, une graine fixe, le cache de prompt désactivé et des cas
de contexte long. Le rapport synthétique de la plateforme de référence est
conservé dans `RESULTS-2026-08-21.md` ; les réponses brutes restent ignorées car
elles peuvent contenir des extraits de diagnostic.
