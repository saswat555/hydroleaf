import os

def write_code_to_file(app_dir, output_file):
    with open(output_file, "w", encoding="utf-8") as out_f:
        # Walk through the app folder recursively
        for root, dirs, files in os.walk(app_dir):
            # Exclude __pycache__ directories from traversal
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as in_f:
                        content = in_f.read()
                except Exception as e:
                    print(f"Could not read file {file_path}: {e}")
                    continue
                # Write a header with the file path then the file content
                out_f.write(f"----- {file_path} -----\n")
                out_f.write(content)
                out_f.write("\n\n")  # Add spacing between files

if __name__ == "__main__":
    app_directory = "app"  # Change this if your app folder is in a different location
    output_filename = "code.txt"
    write_code_to_file(app_directory, output_filename)
    print(f"Code from '{app_directory}' has been written to '{output_filename}'.")
