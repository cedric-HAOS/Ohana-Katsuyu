# CHANGELOG

Toutes les évolutions importantes d'Ohana-Katsuyu sont documentées ici.

## [0.6.1] — Synthèses françaises et références de journaux — 2026-08-24

### Modifié

- La documentation formalise Katsuyu comme worker lourd sous le contrôle
  technique d’Agent, coordonné par Tsunade et vérifié par Shikamaru.
- Le contrôle déterministe renvoie désormais, pour chaque anomalie connue, le
  nombre d’occurrences de référence utilisé pour calculer son évolution.

### Corrigé

- Le moteur local reçoit désormais l’instruction explicite de produire tous les
  champs destinés à l’utilisateur en français.
- Une réponse HTTP d’erreur du runtime IA conserve un extrait borné de son corps
  afin que Tsunade puisse diagnostiquer précisément un rejet HTTP 400.

## [0.6.0] — Analyse IA avancée pour Tsunade — 2026-08-24

### Ajouté

- `ai.inference` produit le contrat d'analyse version 2 : interprétation,
  hypothèses, causes possibles, éléments concordants et contradictoires,
  confiance et investigations recommandées.
- Le schéma JSON strict et le prompt local imposent que toute explication
  causale reste une hypothèse et qu'aucune action ne soit exécutée ou autorisée.
- Le benchmark utilise le même schéma et les mêmes règles épistémiques que le
  handler livré afin d'éviter toute dérive entre évaluation et production.

### Compatibilité

- Le modèle de protocole sait encore lire les résultats historiques version 1 ;
  le runtime 0.6.0 émet systématiquement la version 2.

## [0.5.0] — Analyse déterministe des journaux — 2026-08-24

### Ajouté

- `logs.health_check` récupère directement les journaux HAOS depuis HA-01,
  LINKY-01 et ZWAVE-01, les borne, normalise et groupe sans LLM.
- Le lecteur privilégie le proxy WebSocket natif `supervisor/api`, découvre les
  add-ons teleinfo2mqtt et Z-Wave JS, puis utilise `/api/error_log` comme repli
  explicite si le Supervisor n'est pas accessible.
- `logs.investigate` retourne uniquement une synthèse groupée autour d'un motif
  littéral autorisé par Tsunade, sans persister les lignes correspondantes.
- Les comparaisons distinguent anomalies nouvelles, stables, en hausse, en
  baisse ou disparues ; les rapprochements temporels n'affirment jamais une
  causalité.

## [0.4.1] — Validation des sources INFRA-01 — 2026-08-24

### Corrigé

- `backup.infra` refuse désormais un tar tronqué, sans marqueur de fin, ou ne
  contenant pas toutes les sources Agent, Vision, dnsmasq, chrony et
  `vision.db` attendues.
- Le descripteur protégé est validé contre l'identifiant du job et l'inventaire
  attendu avant toute compression, tout chiffrement et tout envoi à Agent.

## [0.4.0] — Inférence locale optionnelle — 2026-08-24

### Ajouté

- Le handler `ai.inference` démarre un modèle local épinglé uniquement pendant
  le job et retourne un diagnostic strict `OK`, `KO` ou
  `INSUFFICIENT_CONTEXT` sans exécuter d'outil.
- `KatsuyuSetup.exe` propose l'installation de l'IA locale, télécharge de façon
  reprenable le runtime CUDA et le modèle retenu, puis vérifie leurs tailles et
  SHA-256 avant activation.
- Le benchmark reproductible compare qualité, JSON structuré, outils déclaratifs,
  hallucinations, contextes insuffisants, VRAM, RAM, débit et latence.

### Sécurité

- Le moteur écoute uniquement sur `127.0.0.1`, reste absent au repos et ne peut
  choisir ni exécutable, ni modèle, ni schéma, ni commande depuis un job.
- Les archives sont extraites dans un répertoire transitoire confiné ; les
  traversées de chemin et liens symboliques sont refusés.
- Le modèle et le runtime restent protégés par les ACL de Katsuyu. Une mise à
  jour conserve le modèle vérifié et une désinstallation le supprime.

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
