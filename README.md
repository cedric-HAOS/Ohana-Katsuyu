# Ohana-Katsuyu

Ohana-Katsuyu est le worker Windows déterministe exécuté sur Bubule. Il reçoit
uniquement des jobs explicitement autorisés par Ohana-Agent/Tsunade et ne peut
ni créer un job, ni administrer Agent, ni exécuter une commande arbitraire.

Katsuyu est à la fois le nom fonctionnel du worker lourd et son identité
technique. Ohana-Agent reste le runtime de contrôle : Tsunade coordonne et
propose, Agent autorise, Katsuyu exécute un handler déclaré, puis Shikamaru
vérifie l'état observé.

## Capacités du MVP

- `system.health` mesure CPU, mémoire et espace disque de Bubule ;
- `backup.compress` produit un fichier gzip déterministe ;
- `backup.encrypt` chiffre un fichier avec la clé publique `age` fournie ;
- `backup.verify` vérifie un SHA-256 et, facultativement, une taille.
- `backup.infra` récupère le tar INFRA-01 lié au job, le compresse, le chiffre,
  le vérifie et renvoie l'artefact à Agent pour publication distante en flux.
- `logs.health_check` récupère les journaux bornés d’INFRA-01, HA-01,
  LINKY-01 et ZWAVE-01 puis renvoie uniquement une synthèse déterministe ;
- `logs.investigate` analyse un motif ciblé après autorisation de Tsunade et
  renvoie uniquement des signatures normalisées et comptées.

Katsuyu fonctionne sans LLM. Tous les chemins de jobs sont relatifs à
`C:\ProgramData\Ohana\Katsuyu\workspace`. Les chemins absolus, traversées `..`,
liens symboliques et fichiers non réguliers sont refusés.

Pour INFRA-01, Agent transmet uniquement l’extrait journald borné et autorisé
par le job, limité à `ohana-agent.service` et `ohana-vision.service`. Pour HAOS,
la collecte privilégie l'API WebSocket native Home Assistant
`supervisor/api` avec un jeton administrateur. Katsuyu découvre uniquement les
add-ons dont le nom correspond à la cible (`teleinfo2mqtt` ou Z-Wave JS), lit
leurs logs et ceux de Core, puis ferme la connexion. Le volume reste limité à
quatre Mio par cible ; les résultats persistants ne contiennent que les
synthèses groupées, jamais les lignes de journal brutes.

Les analyses sont plafonnées à 200 000 lignes et 64 groupes par source.
`truncated=true` signale une limite d'octets, de lignes ou de groupes atteinte
avec des données omises. Les anomalies absentes ne sont déclarées disparues
que pour une source collectée sans troncature ; cela ne prouve pas la résolution
d'un incident. Un résultat `OK` sans finding sur une collecte tronquée ne
prouve pas l'absence d'anomalies dans les données omises.

## IA locale optionnelle

La capacité `ai.inference` est annoncée uniquement lorsque le runtime et le
modèle local sont configurés. Agent continue de fonctionner normalement sans
elle. Le profil retenu après benchmark sur Bubule est
`Ministral-3-14B-Reasoning-2512` en `Q4_K_M`, exécuté par `llama.cpp` en CUDA.

Le moteur écoute exclusivement sur une boucle locale et n'est démarré que pour
la durée du job. Il est arrêté après succès, échec, annulation ou timeout ; il
n'occupe donc ni VRAM ni RAM lorsqu'aucun incident n'est actif. Le job ne peut
choisir ni modèle, ni exécutable, ni schéma, ni outil. Il reçoit au plus 48 000
caractères de preuves bornées et retourne uniquement `OK`, `KO` ou
`INSUFFICIENT_CONTEXT`, avec constats, contexte manquant, investigation
recommandée et métriques. Le contrat d'analyse version 2 sépare
l'interprétation des hypothèses et exige pour chacune les causes possibles,
éléments concordants et contradictoires et un niveau de confiance.

Le modèle n'exécute aucune recommandation. Les appels d'outils ont été évalués
pendant le benchmark, mais ne sont pas activés dans le handler : Tsunade doit
autoriser séparément toute investigation et la faire exécuter par un handler
déclaré. Le SHA-256 du modèle est vérifié avant sa première utilisation.
Une hypothèse n'est jamais présentée comme un fait et reste soumise à la
décision de Tsunade dans Agent.

Les options locales sont `ai_runtime`, `ai_model`, `ai_model_id`,
`ai_model_sha256` et `ai_context_size` dans `config.json`. Si une partie de ce
groupe est absente, Katsuyu démarre sans annoncer `ai.inference`.

## Installation Windows

L’utilisateur lance uniquement `KatsuyuSetup.exe` en administrateur. Aucun
Python ni `age` ne doit être installé séparément. L’installateur :

1. demande l’adresse d’Ohana-Agent ;
2. récupère le certificat public de l'autorité locale puis valide immédiatement
   l'endpoint HTTPS d'appairage ;
3. affiche un code court et l'empreinte SHA-256 complète à comparer dans
   **Vision > Workers Katsuyu** ;
4. attend l’autorisation explicite dans Vision puis récupère un jeton worker
   individuel, une seule fois ;
5. installe le runtime autonome, `age.exe` et sa licence ; si l'option IA est
   cochée, télécharge directement les composants épinglés (environ 8,3 Gio pour
   le modèle, plus environ 0,5 Gio pour le runtime CUDA), avec reprise et
   contrôle SHA-256 avant activation ;
6. protège le jeton, le certificat public et le workspace par ACL ;
7. teste un véritable enregistrement worker auprès d’Agent ;
8. crée la tâche de démarrage sous `SYSTEM`, lance le worker et installe
   l’icône de notification ;
9. s’inscrit dans la liste Windows des applications installées.

Le worker est installé dans `C:\Program Files\Ohana\Katsuyu`. Son état, ses
logs et son workspace restent sous `C:\ProgramData\Ohana\Katsuyu`. Le jeton
n’apparaît jamais dans une ligne de commande, un log ou le document lu par
l’icône. Toutes les opérations worker utilisent HTTPS sur le port dédié Agent ;
HTTP est refusé par l'installateur et par le worker installé.

La désinstallation Windows arrête Katsuyu, retire le jeton, les exécutables et
les composants IA, mais conserve volontairement les logs et le workspace.

`KatsuyuSetup.exe` détecte l'installation existante, réutilise son adresse
Agent et son jeton, normalise l'identité historique en `katsuyu-bubule`,
arrête proprement le worker et l'icône,
remplace les exécutables avec sauvegarde de retour arrière, puis redémarre
Katsuyu. Les logs, le workspace, l'appairage et le modèle IA vérifié sont
conservés.

À chaque enregistrement, Katsuyu identifie également l'interface Windows
utilisée pour joindre Agent et annonce sa MAC physique pour le Wake-on-LAN.
Cette donnée est découverte sur Bubule ; elle n'a donc plus à être recopiée
manuellement dans la configuration d'Agent.

## Mise à jour

Après son enregistrement auprès d'Agent, le worker consulte au maximum une fois
par 24 heures la dernière release stable du dépôt GitHub officiel
`cedric-HAOS/Ohana-Katsuyu`. Le contrôle utilise un timeout court, ne télécharge
aucun exécutable et ne bloque jamais les jobs en cas d'indisponibilité.

Le résultat borné est conservé dans `status.json`. L'infobulle indique si la
version est à jour, si une mise à jour est disponible ou si le contrôle est
impossible. Dans le deuxième cas, le menu peut ouvrir la page officielle de la
release. Le téléchargement et l'installation restent explicitement déclenchés
par l'utilisateur : aucune mise à jour silencieuse n'est exécutée sous
`SYSTEM`.

## Icône près de l’horloge

L’application de notification est locale et strictement informative. Elle
réutilise l’icône officielle Ohana :

- couleurs normales : worker connecté et disponible ;
- pétales animés dans le sens horaire : job en cours ;
- icône barrée : Agent inaccessible ou erreur ;
- icône grise : worker arrêté ou état local devenu trop ancien.

L’infobulle contient uniquement la version, l'état de connexion explicite, la
dernière connexion et le type du job courant. Chaque interrogation réussie
d'Agent rafraîchit cet état, même lorsqu'aucun job n'est disponible. Le menu
permet d’afficher cet état ou d’ouvrir le dossier des logs. Il ne peut ni
lancer, ni annuler, ni valider une opération.

## Protocole et sécurité

Le protocole v1 réutilise exclusivement les endpoints worker d'Ohana-Agent :

- `GET /v1/jobs/workers/trust` pour amorcer la confiance publique avant tout
  échange de secret ;
- `POST /v1/jobs/workers/pairings` et `.../{id}/poll` pendant l’installation ;
- `POST /v1/jobs/workers/register` ;
- `POST /v1/jobs/claim` ;
- `POST /v1/jobs/{job_id}/heartbeat` ;
- `POST /v1/jobs/{job_id}/complete`.
- `GET /v1/jobs/{job_id}/input` et `POST /v1/jobs/{job_id}/artifact`, uniquement
  pour le propriétaire d'un job `backup.infra` en cours.

Le résultat `backup.infra` inclut la durée, le temps CPU, le pic de mémoire du
processus, les octets logiques lus/écrits, les tailles et les SHA-256. Les
fichiers intermédiaires sont supprimés du workspace après succès ou échec.
Avant toute compression, Katsuyu exige un tar terminé contenant les sources
Agent, Vision, dnsmasq, chrony, l'instantané `vision.db` et un descripteur dont
l'identifiant et l'inventaire correspondent exactement au job.

Les heartbeats publient la progression, renouvellent le bail et retournent
l'état courant. Un état `CANCELLED` ou `TIMEOUT` interrompt le handler à son
prochain point sûr. Après une perte de connexion, le traitement est arrêté et
Agent récupère le job à l'expiration du bail. Les sorties déjà publiées dans le
workspace sont vérifiées et réutilisées lors d'une reprise.

Le certificat public téléchargé n'est accepté définitivement qu'après la
comparaison humaine de son empreinte avec Vision. Katsuyu utilise ensuite un
contexte TLS qui exige cette autorité et vérifie le nom DNS ou l'adresse IP du
certificat serveur. Aucun mode `verify=False` n'est utilisé après l'amorçage.

Les logs tournent à 5 Mio avec trois archives. Aucun journal distant n'est
centralisé par Katsuyu.

## Construction de l’installateur

Le build Windows utilise Python uniquement côté publication :

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[development,windows-build]"
.\scripts\build_windows.ps1
```

Le script télécharge l’archive officielle `age` v1.3.1 pour Windows amd64,
vérifie son SHA-256 figé, construit les trois exécutables autonomes puis
embarque le worker, l’icône et `age.exe` dans un unique `KatsuyuSetup.exe`.
Il ajoute les métadonnées Windows de version et génère `dist\SHA256SUMS` pour
le setup final.

## Fin du cycle de journaux (0.8.6)

Mettre Agent à jour en 1.26.16 avant Katsuyu. Le worker transmet les journaux,
puis prend les diagnostics complémentaires décidés par Tsunade. L’arrêt n’est
autorisé que par un polling à vide après traitement des résultats, avec la
politique d’arrêt activée et un réveil attribué à Ohana. Un démarrage manuel
ne déclenche pas cet arrêt. Les délais IA restent bornés à 900 secondes.

Avec un ancien Agent, seul un HTTP 404 provoque le retour au polling historique.
Les tâches restent exécutables mais l’ancien indicateur d’arrêt est ignoré.
Aucune nouvelle configuration YAML n’est nécessaire.
