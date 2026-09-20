# CHANGELOG

## Non publié

- Les collectes générales et ciblées conservent les groupes critiques avant
  les autres lors du plafonnement à 64 groupes, même avec une seule occurrence.
  Les messages `CRITICAL` et `FATAL` sont détectés sans nécessiter un autre mot
  d'erreur. Les compteurs et le signalement de troncature restent conservés.

- Les collectes Supervisor demandent une ligne témoin au-delà du plafond de
  10 000 lignes et distinguent un volume exactement égal au plafond d'octets
  d'un dépassement. Le résultat signale les pertes de données.
- Le repli sur les journaux Core après échec Supervisor reste explicitement
  incomplet. Le message d'erreur brut n'est plus intégré aux preuves : seul
  le type d'erreur est conservé, sans URL ni identifiant exposé par l'exception.

- Le contrôle général des journaux signale `truncated=true` lorsque l'analyse
  dépasse 200 000 lignes ou 64 groupes, même si la collecte n'a pas atteint sa
  limite d'octets. Une anomalie absente n'est déclarée disparue que pour une
  source effectivement collectée sans troncature ; un contrôle partiel ne
  déclare plus disparues les anomalies des autres sources.

- Sur ZWAVE-01, les messages INFO exacts de début de vérification des firmwares
  et de démarrage du stockage des sauvegardes ne créent plus seuls une anomalie
  ni une corrélation. Les variantes d'échec, les niveaux WARNING/ERROR et les
  autres sources conservent leur détection ; les correspondances ciblées restent
  comptées. Les messages de cycle des services `s6-rc` ne sont pas modifiés.

- Le message INFO exact de déconnexion d'un client Z-WAVE-SERVER sur ZWAVE-01
  ne constitue plus à lui seul une anomalie ni une corrélation. Les erreurs,
  avertissements et messages comportant un contexte d'échec restent détectés.
  Une recherche ciblée conserve le nombre de lignes correspondantes.

- Les dates ISO des journaux sont retirées avant conversion en minuscules.
  Un changement de jour ou d'heure ne crée plus une nouvelle signature pour
  le même message. Les dates réelles des observations restent conservées.

## Non publié

- Les paramètres de session caméra dans les chemins `/stok=…/` sont masqués
  avant regroupement des journaux et extraction des références. Les anciennes
  signatures de référence sont également masquées et regroupées pour conserver
  la comparaison des occurrences sans signaler une fausse nouveauté.

## [0.8.7] — 2026-09-15 — Diagnostics et journaux fiables

- Une réponse IA ne respectant pas le schéma est régénérée une seule fois avec
  les mêmes preuves et les contraintes invalides, sans tronquer les conclusions.
- Les termes MQTT, série ou frame dans un message normal ne suffisent plus à
  créer une anomalie. Une collecte ciblée distingue correspondance et anomalie.
- L’installeur accepte `--update-existing` pour une installation déjà appairée,
  avec les droits administrateur habituels et conservation de son identité.

## [0.8.6] — 2026-09-11 — Arrêt après le cycle complet

- Katsuyu utilise `/v1/jobs/next` et attend une autorisation d’arrêt explicite
  après le traitement des résultats et des éventuels diagnostics complémentaires.
- L’ancien indicateur reçu avant exécution ne déclenche plus l’arrêt. Avec un
  ancien Agent, le polling historique reste utilisable, sans arrêt automatique.

Toutes les évolutions importantes d'Ohana-Katsuyu sont documentées ici.

## [0.8.4] — Journaux INFRA-01 — 2026-08-29

### Ajouté

- `logs.health_check` et `logs.investigate` acceptent la source `infra-01`
  transmise de façon bornée par Agent.
- L’analyse déterministe reconnaît les arrêts, démarrages, demandes d’arrêt et
  refus de connexion afin de corréler une indisponibilité Agent avec ses effets.

## [0.8.3] — JSON d’analyse fiable — 2026-08-27

### Corrigé

- Les diagnostics structurés désactivent la réflexion interne du modèle afin
  de réserver la sortie au JSON attendu par Tsunade.
- La fenêtre locale passe à 32 768 jetons, Tsunade en demande jusqu’à 8 192 et
  le contrat accepte jusqu’à 16 384 jetons pour les analyses plus développées.
- Katsuyu réduit automatiquement la sortie lorsque les preuves occupent une
  part plus importante de la fenêtre de contexte.
- Une mise à jour réutilise le modèle déjà vérifié sans téléchargement inutile
  lorsque seule la taille de contexte change.
- Katsuyu distingue désormais une réponse vide, une troncature à la limite de
  jetons et un JSON réellement invalide dans l’erreur remontée à Agent.

## [0.8.2] — Références Home Assistant exploitables — 2026-08-27

### Corrigé

- L’analyse déterministe des journaux conserve séparément les identifiants
  d’entité Home Assistant présents dans chaque anomalie groupée.
- Tsunade peut ainsi produire une vérification ciblée sans exposer la ligne de
  journal brute ni dépendre de l’état instantané du capteur.

## [0.8.1] — Compatibilité IA llama.cpp — 2026-08-27

### Corrigé

- Le schéma envoyé au runtime local `llama.cpp` est simplifié afin d'éviter
  l'erreur `Failed to initialize samplers: failed to parse grammar` observée
  avant génération sur les jobs `ai.inference`.
- Katsuyu conserve le contrat strict de validation locale après génération :
  les tailles, motifs et cohérences du diagnostic restent vérifiés par
  Pydantic avant retour à Tsunade.
- Le benchmark IA utilise désormais le même schéma runtime que le handler de
  production pour éviter une divergence entre validation et exécution réelle.

## [0.7.0] — Identité canonique et Wake-on-LAN — 2026-08-25

### Ajouté

- Katsuyu détecte l'adresse MAC physique de l'interface Windows réellement
  utilisée pour joindre Agent et l'annonce avec ses capacités lors de
  l'enregistrement worker.
- L'enregistrement peut annoncer explicitement l'ancien `worker_id` pendant
  une migration afin qu'Agent conserve le jeton d'appairage existant.

### Modifié

- Les nouvelles identités générées à partir du nom Windows sont normalisées en
  minuscules ; `Bubule` devient donc `katsuyu-bubule`.

### Compatibilité

- Une installation existante `katsuyu-Bubule` est convertie lors de la mise à
  jour sans nouvel appairage, à condition qu'Agent 1.25.0 ou supérieur soit
  installé avant Katsuyu 0.7.0.

## [0.6.3] — Collecte des journaux HAOS — 2026-08-24

### Corrigé

- Les journaux texte Home Assistant et add-ons sont récupérés via le proxy HTTP
  `/api/hassio/...`, au lieu d'être décodés à tort comme du JSON par
  `supervisor/api`.
- Le WebSocket Supervisor reste limité à la découverte JSON des add-ons.
- En cas de repli, Katsuyu conserve désormais la cause initiale de l'échec de
  collecte au lieu de ne remonter que l'erreur HTTP secondaire.

## [0.6.2] — Diagnostic des transferts de sauvegarde — 2026-08-24

### Corrigé

- Lorsqu’Agent refuse la source d’une sauvegarde distribuée, Katsuyu conserve
  le détail JSON borné de la réponse HTTP et le restitue en français dans le
  résultat du job et l’incident Tsunade.
- Les erreurs réseau distinguent désormais un refus Agent d’une indisponibilité
  de transport, sans exposer de contenu non borné.

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
