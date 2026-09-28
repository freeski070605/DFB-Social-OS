import argparse
import getpass
from filelock import FileLock
from app.core.config import ROOT
from app.db.session import SessionLocal
from app.brands.service import seed
from app.security.auth import create_admin
from app.services.backup import backup, restore


def main():
    parser = argparse.ArgumentParser(description="DFB Social OS administration")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin")
    admin.add_argument("--username", default="admin")
    sub.add_parser("seed")
    sub.add_parser("backup")
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("archive")
    args = parser.parse_args()
    if args.command == "backup":
        print(backup())
    elif args.command == "restore":
        with FileLock(ROOT / "data/application.lock", timeout=0):
            print("Pre-restore backup:", backup())
            print("Restored with autopilot paused:", restore(args.archive))
    else:
        with SessionLocal() as db:
            if args.command == "seed":
                seed(db)
            else:
                password = getpass.getpass("New administrator password (12+ characters): ")
                if password != getpass.getpass("Confirm password: "):
                    raise SystemExit("Passwords do not match")
                create_admin(db, args.username, password)
            db.commit()
        print("Done")


if __name__ == "__main__":
    main()
