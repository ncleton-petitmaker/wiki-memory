# Wiki Memory

Wiki Memory est un moteur de mémoire MIT, local-first, self-hosted et extensible par plugins. Il fonctionne seul, hors ligne, sans compte. Le bundle optionnel `team` ajoute OIDC, espaces partagés, ACL, revue, audit et réplication ; il n’est jamais une dépendance du mode solo.

Version actuelle : `1.0.0-alpha.16`. La fondation V1 est exécutable, mais le tag stable `1.0.0` reste conditionné à une restauration sur l’infrastructure de production et à un audit externe. L’état exact des preuves est consigné dans [Évidence de release](docs/RELEASE_EVIDENCE.md).

## Ce qui est canonique

```text
source → blob SHA-256 durable → événement SQLite append-only → projections
                                                     ├─ Markdown / Obsidian
                                                     ├─ recherche QMD
                                                     └─ faits et synthèses
```

Les preuves originales et le journal sont la vérité. Markdown reste lisible et modifiable, mais c’est une projection reconstruisible. Une édition manuelle est détectée par hash, conservée comme preuve et proposée à la revue ; elle n’est jamais écrasée silencieusement.

Chaque événement conserve l’acteur, les deux dates (`occurredAt` et `recordedAt`), la version de flux, la clé d’idempotence, le plugin et sa version exacte, les preuves SHA-256, la portée et l’ACL. Une preuve est fsyncée avant l’événement qui la référence. Une clé d’idempotence réutilisée avec un autre contenu est refusée.

## Démarrage solo

```bash
python -m pip install -e .
wiki-memory init ./Mémoire --spec ./onboarding.json
wiki-memory profile-doctor ./Mémoire --profile solo
wiki-memory capture ./Mémoire --vault knowledge --text "Décision sourcée"
wiki-memory query ./Mémoire "quelle décision ?"
wiki-memory verify ./Mémoire
```

Le profil solo active Core, la projection Markdown, QMD, Docling, le connecteur social, MCP, les packs Syncthing et la sauvegarde locale. L’API HTTP locale refuse toute écoute hors loopback et utilise le trousseau système, avec un fichier `0600` hors mémoire en fallback.

## Plugins

Un plugin déclare capacités, dépendances, runtime, permissions, secrets, classes de données, schéma de configuration, health check et délai d’arrêt. Son cycle de vie est visible :

```text
DISCOVERED → PENDING → STARTING → ACTIVE → DRAINING → STOPPED
                         ↘ FAILED / QUARANTINED
```

Les acquisitions sont nettoyées dans l’ordre inverse si le démarrage échoue. Les plugins Python non approuvés sont mis en quarantaine hors mode développeur ; les runtimes `executable` et `oci` ne sont jamais chargés dans le processus Core. Le kit de conformité vérifie les manifests, schémas, capacités et états :

```bash
python scripts/schema_validate.py
python scripts/plugin_conformance.py
```

Contrats et création d’un connecteur : [Plugin SDK](docs/PLUGIN_SDK.md).

## Sources officielles V1

- fichiers, URLs et texte ;
- navigateur social, sans contourner les contrôles d’accès ;
- audio MP3/M4A/WAV avec original immuable, transcription locale `whisper.cpp` ou Mistral explicite, timestamps et révisions ;
- PostgreSQL read-only avec allowlists obligatoires de schémas, tables et colonnes, snapshot/cursor avec recouvrement idempotent, et adaptateur Debezium CDC ;
- Docling pour les documents structurés ;
- packs d’événements et blobs pour Syncthing — jamais le SQLite actif.

Plaud n’est pas une dépendance : un export audio Plaud est ingéré par le connecteur audio et conserve Plaud comme provenance. Aucun audio ne part sur Internet sans choix explicite d’un provider réseau.

Tous les `SourceConnector` suivent les mêmes commandes :

```bash
wiki-memory connector-check ./Mémoire --plugin source-social-browser --config social.json
wiki-memory connector-discover ./Mémoire --plugin source-social-browser --config social.json
wiki-memory connector-sync ./Mémoire --plugin source-social-browser --config social.json \
  --selection selection-social.json --vault knowledge --instance navigateur-poste
```

Un manifeste tiers explicite suit ce même parcours. En solo, un plugin Python
tiers exige `--developer-mode`; les connecteurs `executable` et `oci` restent
isolés par capacité. Les secrets nommés passent uniquement par
`--secret-env SECRET=VARIABLE_ENV`, jamais par le fichier de configuration.
Avec `--profile team-client`, utilisez aussi `--profile-config` pour fournir
la configuration `serverUrl` du plugin `team-client`.

## Team optionnel

Team fournit une API FastAPI, PostgreSQL, S3/MinIO, OIDC, RBAC+ACL, revue, audit, recherche préfiltrée par autorisation, worker transactionnel `FOR UPDATE SKIP LOCKED`, outbox hors ligne et console `/console`.

```bash
cd deploy/team
docker compose up -d
```

Le Compose n’expose l’API que sur `127.0.0.1:8787`; placez un reverse proxy TLS devant elle. Le chart Helm se trouve dans `deploy/helm/wiki-memory`. Consultez [Team self-hosting](docs/TEAM_SELF_HOSTING.md) avant toute production : OIDC, sauvegardes PITR, versioning objet et restauration testée sont obligatoires.

Comportements par défaut : « garde ça » reste privé ; une publication partagée exige un aperçu exact et son hash ; les sources d’équipe sont des preuves, les faits extraits sont des propositions ; les conflits de version créent une proposition, jamais une fusion silencieuse.

## Interfaces agents

Le moteur expose les mêmes primitives en HTTP local et Team : captures, événements, blobs, recherche, propositions, revue et santé. La façade MCP expose `memory_capture`, `memory_search`, `memory_get_evidence`, `memory_propose_change`, `memory_publish` et, pour les curateurs, `memory_review`.

```bash
wiki-memory mcp-serve ./Mémoire
wiki-memory serve ./Mémoire
wiki-memory team-sync ./Mémoire --server https://memory.example
```

## Sauvegarde, reconstruction et transport

```bash
wiki-memory backup ./Mémoire ./backup.tar.gz
wiki-memory backup-verify ./backup.tar.gz
wiki-memory backup-restore ./backup.tar.gz ./Mémoire-restaurée
wiki-memory rebuild ./Mémoire
wiki-memory event-pack-export ./Mémoire
wiki-memory event-pack-import ./Mémoire ./pack.json
```

Les sauvegardes utilisent l’API SQLite backup, manifestent et vérifient chaque fichier, refusent les liens et traversées de chemin, puis contrôlent l’intégrité et le nombre d’événements. Les packs sont immuables, hashés et importés de manière idempotente après arrivée de leurs blobs.

## Limites honnêtes de l’alpha

- le serveur Team utilise PostgreSQL FTS ; la recherche vectorielle serveur n’est pas encore livrée ;
- la recherche du cache Team hors ligne est lexicale et filtrée avant lecture ; les embeddings QMD restent volontairement limités aux coffres privés ;
- les plugins `executable` et `oci` passent par un hôte NDJSON à capacités limitées. Un exécutable est isolé du Core mais pas sandboxé au niveau de l’OS ; Team doit exiger un OCI signé pour les connecteurs hostiles ;
- les migrations forward propres au plugin sont durables et rejouables après crash, pour les plugins Python, `executable` et `oci` ; une nouvelle version saine est démarrée en staging, échange atomiquement toutes ses capacités, puis draine/redémarre ses dépendants ;
- l’adaptateur Debezium est livré, mais l’exploitation du transport Kafka/Connect reste externe ;
- le chart crée PostgreSQL/MinIO pour évaluation, mais une production doit fournir HA, PITR et une restauration automatisée externe ;
- la répétition 500 membres/100 connecteurs et la répétition WAL Team synthétique sont implémentées et exécutées. Le PITR et la restauration d’un object store versionné sur l’infrastructure réelle de l’opérateur, ainsi qu’un audit externe des autorisations/isolation, restent des gates de release stable. La durabilité solo des événements acquittés possède une campagne `kill -9` reproductible, à exécuter sur chaque système de fichiers supporté avant le tag stable ;
- aucun graphe canonique ni CRDT sémantique n’est inclus.

## Développement

```bash
python -m unittest discover -s tests -v
python -m compileall -q src scripts tests
python scripts/schema_validate.py
python scripts/plugin_conformance.py
python scripts/privacy_scan.py .
```

CI couvre Python 3.10/3.12 sur Linux, macOS et Windows, PostgreSQL réel, scan de secrets, SBOM, provenance et signatures Cosign des releases et images GHCR. Seules des fixtures synthétiques sont admises dans GitHub ; aucune mémoire utilisateur n’y transite.

Documentation : [Architecture](docs/ARCHITECTURE.md) · [Plugin SDK](docs/PLUGIN_SDK.md) · [Team](docs/TEAM_SELF_HOSTING.md) · [Fiabilité](docs/RELIABILITY.md) · [Vérifier une release](docs/VERIFY_RELEASE.md) · [Évidence de release](docs/RELEASE_EVIDENCE.md) · [CLI](docs/CLI_REFERENCE.md) · [Sécurité](SECURITY.md).

Licence : [MIT](LICENSE).
