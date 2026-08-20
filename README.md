# Ohana-Katsuyu

Ohana-Katsuyu est le worker Windows déterministe exécuté sur Bubule. Il reçoit
uniquement des jobs explicitement autorisés par Ohana-Agent/Tsunade et ne peut
ni créer un job, ni administrer Agent, ni exécuter une commande arbitraire.

## Capacités du MVP

- `system.health` mesure CPU, mémoire et espace disque de Bubule ;
- `backup.compress` produit un fichier gzip déterministe ;
- `backup.encrypt` chiffre un fichier avec la clé publique `age` fournie ;
- `backup.verify` vérifie un SHA-256 et, facultativement, une taille.
- `backup.infra` récupère le tar INFRA-01 lié au job, le compresse, le chiffre,
  le vérifie et renvoie l'artefact à Agent pour publication distante en flux.

Katsuyu fonctionne sans LLM. Tous les chemins de jobs sont relatifs à
`C:\ProgramData\Ohana\Katsuyu\workspace`. Les chemins absolus, traversées `..`,
liens symboliques et fichiers non réguliers sont refusés.

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
5. installe le runtime autonome, `age.exe` et sa licence ;
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

La désinstallation Windows arrête et désenregistre Katsuyu, retire le jeton et
les exécutables, mais conserve volontairement les logs et le workspace.

Une version ultérieure de `KatsuyuSetup.exe` détecte l'installation existante,
réutilise son adresse Agent, son identité et son jeton, arrête proprement le
worker et l'icône, remplace les exécutables avec sauvegarde de retour arrière,
puis redémarre Katsuyu. Les logs, le workspace et l'appairage sont conservés.

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
