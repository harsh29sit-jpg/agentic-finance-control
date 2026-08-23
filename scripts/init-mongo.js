// init-mongo.js — runs once against a fresh, auth-less mongod via
// docker-entrypoint-initdb.d. Creates:
//   1. root admin (from env)
//   2. app user scoped to DB_NAME with readWrite
//   3. backup user with cluster-wide backup role (for the sidecar)
// All passwords come from the environment — never hardcode.

const rootUser = process.env.MONGO_INITDB_ROOT_USERNAME;
const rootPass = process.env.MONGO_INITDB_ROOT_PASSWORD;
const appName = process.env.MONGO_APP_USER || "recon_app";
const appPass = process.env.MONGO_APP_PASSWORD;
const backupUser = process.env.MONGO_BACKUP_USER || "recon_backup";
const backupPass = process.env.MONGO_BACKUP_PASSWORD;
const dbName = process.env.DB_NAME || "recon_control_tower";

if (!rootPass || !appPass || !backupPass) {
  throw new Error("MONGO_*_PASSWORD env vars are required");
}

db = db.getSiblingDB("admin");
db.createUser({
  user: rootUser,
  pwd: rootPass,
  roles: [{ role: "root", db: "admin" }],
});

db = db.getSiblingDB(dbName);
db.createUser({
  user: appName,
  pwd: appPass,
  roles: [
    { role: "readWrite", db: dbName },
    { role: "dbAdmin", db: dbName },
  ],
});

db = db.getSiblingDB("admin");
db.createUser({
  user: backupUser,
  pwd: backupPass,
  roles: [{ role: "backup", db: "admin" }],
});

print("mongo users initialised");
