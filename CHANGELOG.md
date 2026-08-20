# CHANGELOG

Toutes les évolutions importantes d'Ohana-Katsuyu sont documentées ici.

## [0.1.0] — Katsuyu MVP — 2026-08-20

### Ajouté

- Enregistrement authentifié auprès d'Ohana-Agent et annonce des capacités.
- Réception, progression, annulation, timeout et résultat des jobs v1.
- Handlers déterministes `system.health`, `backup.compress`, `backup.encrypt`
  et `backup.verify`, sans LLM ni shell arbitraire.
- Espace de travail confiné, logs rotatifs et démarrage automatique Windows.
- Appairage temporaire approuvé dans Vision avec jeton individuel par worker.
- Installateur autonome, désinstallation Windows et application informative
  près de l’horloge avec l’icône officielle Ohana.
- Build monofichier incluant l’archive `age` v1.3.1 vérifiée et sa licence.
- Contrôle quotidien borné de la dernière release stable, sans dépendance pour
  l'exécution des jobs et sans installation silencieuse.
- État de mise à jour synthétique dans l'infobulle et accès à la release depuis
  l'icône informative.
- Mise à niveau en place préservant le jeton, la configuration, les logs et le
  workspace, avec sauvegarde locale de retour arrière des exécutables.
- Métadonnées de version Windows et `SHA256SUMS` générés pendant le build.
