# CHANGELOG

Toutes les évolutions importantes d'Ohana-Katsuyu sont documentées ici.

## [0.3.1] — État de connexion fiable — 2026-08-20

### Corrigé

- Chaque interrogation réussie d'Agent rafraîchit maintenant l'état local,
  même lorsqu'aucun job n'est disponible ; l'icône reste donc en couleurs
  pendant les périodes d'inactivité normales.
- L'infobulle distingue explicitement un worker connecté, arrêté, en erreur ou
  dont l'état local est périmé.

## [0.3.0] — Sauvegarde INFRA-01 déterministe — 2026-08-20

### Ajouté

- `backup.infra` récupère le tar lié au job, le compresse, le chiffre avec
  `age`, calcule ses SHA-256 et renvoie l'artefact à Agent en flux.
- Le résultat mesure durée, temps CPU, pic mémoire et volumes logiques d'I/O.

### Sécurité

- Les flux exigent le jeton individuel, le worker propriétaire et la tentative
  courante ; les paramètres n'acceptent ni commande ni chemin.
- Tous les fichiers intermédiaires restent dans le workspace protégé et sont
  supprimés après succès, annulation, timeout ou échec.

## [0.2.1] — Démarrage Windows à commande courte — 2026-08-20

### Corrigé

- La tâche de démarrage ne sérialise plus tous les chemins et paramètres dans
  `/TR`, limité à 261 caractères par Windows Task Scheduler.
- Le worker lit désormais les paramètres bornés depuis le `config.json`
  sécurisé déjà produit par l'installateur ; la tâche ne transmet que son
  chemin avec `--config-file`.

## [0.2.0] — Appairage HTTPS épinglé — 2026-08-20

### Ajouté

- L'installateur récupère l'autorité publique d'Agent, affiche son empreinte
  SHA-256 avec le code d'appairage puis conserve le certificat dans l'état
  sécurisé du worker.
- Le worker réutilise ce certificat épinglé pour toutes ses communications
  HTTPS avec Agent.

### Sécurité

- Les adresses Agent en HTTP, avec identifiants, chemin ou paramètres sont
  refusées ; le port worker 8766 est proposé par défaut.
- Aucun secret n'est envoyé avant validation du nom d'hôte avec l'autorité
  récupérée, et l'empreinte retournée par l'appairage doit correspondre.
- Une installation antérieure en HTTP ne peut pas être réutilisée silencieusement
  et doit faire l'objet d'un nouvel appairage sécurisé.

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
