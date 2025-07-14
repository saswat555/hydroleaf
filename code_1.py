#!/usr/bin/env python3
import os
from typing import List

def write_code_to_file(directories: List[str], output_file: str):
    # only dump these extensions
    allowed_exts = {'.py', '.env', '.txt', '.md', '.ino'}  # include Arduino .ino files
    # directories to skip entirely
    skip_dirs = {
        '__pycache__', '.git', 'static', 'templates', 'alembic', '.venv', 'migrations'
    }

    with open(output_file, "w", encoding="utf-8", errors="ignore") as out_f:
        for app_dir in directories:
            if not os.path.isdir(app_dir):
                print(f"Directory '{app_dir}' does not exist. Skipping.")
                continue

            out_f.write(f"======= Directory: {app_dir} =======\n\n")
            for root, dirs, files in os.walk(app_dir):
                # prune out unwanted directories
                dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]

                for fname in files:
                    # skip hidden and metadata files
                    if fname.startswith('.') or fname == '.DS_Store':
                        continue

                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in allowed_exts:
                        continue

                    file_path = os.path.join(root, fname)
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as in_f:
                            content = in_f.read()
                    except Exception as e:
                        print(f"Could not read file {file_path}: {e}")
                        continue

                    out_f.write(f"----- {file_path} -----\n")
                    out_f.write(content)
                    out_f.write("\n\n")  # spacing between files

if __name__ == "__main__":
    # List all directories to aggregate code from
    directories = ["app"]
    output_filename = "code.txt"
    write_code_to_file(directories, output_filename)
    print(f"Code from directories {directories} has been written to '{output_filename}'.")
