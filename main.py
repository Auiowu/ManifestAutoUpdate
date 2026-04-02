import os
import git
import sys
import json
import time
import base64
import gevent
import logging
import argparse
import platform
import requests
import functools
import traceback
import subprocess
from pathlib import Path
from threading import Lock
from typing import Optional, Dict, List, Any
from steam.enums import EResult
from push import push, push_data
from multiprocessing.pool import ThreadPool
from multiprocessing.dummy import Pool
from steam.guard import generate_twofactor_code
from DepotManifestGen.main import MySteamClient, MyCDNClient, get_manifest, BillingType, Result

# Global thread-safe lock
_global_lock = Lock()

# Increase recursion limit with reasonable bounds (not 10 million)
sys.setrecursionlimit(100000)

# Command-line argument parser
parser = argparse.ArgumentParser(description='Steam Manifest Auto Update Tool')
parser.add_argument('-c', '--credential-location', default=None, help='Credential location path')
parser.add_argument('-l', '--level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='Logging level')
parser.add_argument('-p', '--pool-num', type=int, default=8, help='Number of thread pool workers')
parser.add_argument('-r', '--retry-num', type=int, default=3, help='Number of retry attempts')
parser.add_argument('-t', '--update-wait-time', type=int, default=86400, help='Wait time between updates in seconds')
parser.add_argument('-k', '--key', default=None, help='Encryption key')
parser.add_argument('-i', '--init-only', action='store_true', default=False, help='Only initialize, do not run updates')
parser.add_argument('-C', '--cli', action='store_true', default=False, help='Use CLI for interactive login')
parser.add_argument('-P', '--no-push', action='store_true', default=False, help='Skip git push operations')
parser.add_argument('-u', '--update', action='store_true', default=False, help='Check for updates')
parser.add_argument('-a', '--app-id', dest='app_id_list', action='extend', nargs='*', help='Specific app IDs to update')
parser.add_argument('-U', '--users', dest='user_list', action='extend', nargs='*', help='Specific users to update')


class MyJson(dict):
    """JSON file wrapper for persistent storage with auto-loading/dumping."""
    
    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self.load()
    
    def load(self) -> None:
        """Load JSON from file or create if not exists."""
        if not self.path.exists():
            self.dump()
            return
        try:
            with self.path.open('r', encoding='utf-8') as f:
                self.update(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            logging.warning(f"Failed to load JSON from {self.path}: {e}")
            self.dump()
    
    def dump(self) -> None:
        """Save JSON to file."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('w', encoding='utf-8') as f:
                json.dump(dict(self), f, indent=2)
        except IOError as e:
            logging.error(f"Failed to dump JSON to {self.path}: {e}")


class SafeExceptionHandler:
    """Decorator for safe exception handling in async tasks."""
    
    def __init__(self, func):
        self.func = func
        functools.update_wrapper(self, func)
    
    def __call__(self, *args, **kwargs):
        try:
            return self.func(*args, **kwargs)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logging.error(f"Error in {self.func.__name__}: {traceback.format_exc()}")
            return None


class ManifestAutoUpdate:
    """Main class for automated Steam manifest updates."""
    
    log = logging.getLogger('ManifestAutoUpdate')
    ROOT = Path('data').absolute()
    
    # File paths
    users_path = ROOT / 'users.json'
    app_info_path = ROOT / 'appinfo.json'
    user_info_path = ROOT / 'userinfo.json'
    two_factor_path = ROOT / '2fa.json'
    appuserlist_path = ROOT / 'appuserlist.json'
    key_path = ROOT / 'KEY'
    git_crypt_path = ROOT / ('git-crypt' + ('.exe' if platform.system().lower() == 'windows' else ''))
    
    def __init__(
        self,
        credential_location: Optional[str] = None,
        level: Optional[str] = None,
        pool_num: Optional[int] = None,
        retry_num: Optional[int] = None,
        update_wait_time: Optional[int] = None,
        key: Optional[str] = None,
        init_only: bool = False,
        cli: bool = False,
        app_id_list: Optional[List[str]] = None,
        user_list: Optional[List[str]] = None,
    ):
        """Initialize the manifest auto-update system."""
        # Setup logging
        log_level = logging.getLevelName((level or 'INFO').upper())
        logging.basicConfig(
            format='%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s',
            level=log_level
        )
        logging.getLogger('MySteamClient').setLevel(logging.WARNING)
        
        # Initialize attributes
        self.init_only = init_only
        self.cli = cli
        self.users = 0
        self.invalid_password_count = 0
        self.pool_num = pool_num or 8
        self.retry_num = retry_num or 3
        self.update_wait_time = update_wait_time or 86400
        self.credential_location = Path(credential_location or self.ROOT / 'client')
        self.key = key
        self.app_sha = None
        self.app_lock: Dict[int, set] = {}
        self.remote_head: Dict[str, str] = {}
        self.tags: set = set()
        
        # Initialize git repo
        try:
            self.repo = git.Repo()
        except git.InvalidGitRepositoryError:
            self.log.error("Current directory is not a valid git repository")
            sys.exit(1)
        
        # Initialize git branches and encryption
        self._initialize_git_branches()
        self._initialize_encryption()
        
        # Load JSON configs
        self.account_info = MyJson(self.users_path)
        self.user_info = MyJson(self.user_info_path)
        self.app_info = MyJson(self.app_info_path)
        self.two_factor = MyJson(self.two_factor_path)
        self.appuserlist = MyJson(self.appuserlist_path)
        
        # Setup update lists
        self.log.info('Fetching remote tags...')
        self.get_remote_tags()
        self.update_user_list = list(set(user_list or []))
        self.update_app_id_list = []
        
        if app_id_list:
            self.update_app_id_list = list(set(
                int(i) for i in app_id_list if i.isdecimal()
            ))
            self._filter_users_by_app_id()
    
    def _initialize_git_branches(self) -> None:
        """Initialize required git branches (app and data)."""
        try:
            if not self.check_app_repo_local('app'):
                if self.check_app_repo_remote('app'):
                    self.log.info('Pulling remote app branch...')
                    self.repo.git.fetch('origin', 'app:app')
                else:
                    try:
                        self.log.info('Fetching full repository history...')
                        self.repo.git.fetch('--unshallow')
                    except git.exc.GitCommandError as e:
                        self.log.debug(f'Unshallow fetch failed: {e}')
                    
                    self.app_sha = self.repo.git.rev_list('--max-parents=0', 'HEAD').strip()
                    self.repo.git.branch('app', self.app_sha)
            
            if not self.app_sha:
                self.app_sha = self.repo.git.rev_list('--max-parents=0', 'app').strip()
            
            if not self.check_app_repo_local('data'):
                if self.check_app_repo_remote('data'):
                    self.log.info('Pulling remote data branch...')
                    self.repo.git.fetch('origin', 'data:origin_data')
                    self.repo.git.worktree('add', '-b', 'data', 'data', 'origin_data')
                else:
                    self.repo.git.worktree('add', '-b', 'data', 'data', 'app')
        except git.exc.GitCommandError as e:
            self.log.error(f"Git initialization failed: {e}")
            sys.exit(1)
    
    def _initialize_encryption(self) -> None:
        """Initialize git-crypt encryption."""
        try:
            data_repo = git.Repo('data')
            
            if data_repo.head.commit.hexsha == self.app_sha:
                self.log.info('Initializing data branch encryption...')
                self.download_git_crypt()
                subprocess.run([self.git_crypt_path, 'init'], cwd='data', check=True)
                subprocess.run([self.git_crypt_path, 'export-key', self.key_path], cwd='data', check=True)
                
                with self.key_path.open('rb') as f:
                    self.key = f.read().hex()
                
                self.log.info(f'Key exported. Add to Repository secrets: {self.key}')
                self._setup_git_attributes(data_repo)
            
            if self.key and self._is_encrypted_file(self.users_path):
                self._unlock_encrypted_files()
            
            if not self.credential_location.exists():
                self.credential_location.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log.error(f"Encryption initialization failed: {e}")
    
    def _is_encrypted_file(self, file_path: Path) -> bool:
        """Check if file is encrypted by git-crypt."""
        if not file_path.exists() or file_path.stat().st_size == 0:
            return False
        try:
            with file_path.open('rb') as f:
                return f.read(10) == b'\x00GITCRYPT\x00'
        except IOError:
            return False
    
    def _unlock_encrypted_files(self) -> None:
        """Unlock encrypted files using git-crypt."""
        try:
            self.download_git_crypt()
            with self.key_path.open('wb') as f:
                f.write(bytes.fromhex(self.key))
            subprocess.run([self.git_crypt_path, 'unlock', self.key_path], cwd='data', check=True)
            self.log.info('Git-crypt unlocked successfully')
        except Exception as e:
            self.log.error(f"Failed to unlock encrypted files: {e}")
    
    def _setup_git_attributes(self, data_repo: git.Repo) -> None:
        """Configure .gitattributes for git-crypt."""
        try:
            gitattributes = self.ROOT / '.gitattributes'
            encrypted_files = ['users.json', 'client/*.key', '2fa.json']
            content = '\n'.join(f'{f} filter=git-crypt diff=git-crypt' for f in encrypted_files)
            
            with gitattributes.open('w') as f:
                f.write(content)
            
            data_repo.git.add('.gitattributes')
        except IOError as e:
            self.log.error(f"Failed to setup .gitattributes: {e}")
    
    def _filter_users_by_app_id(self) -> None:
        """Filter users that have the specified app IDs."""
        for user, info in self.user_info.items():
            if info.get('enable') and info.get('app'):
                if any(app_id in self.update_app_id_list for app_id in info['app']):
                    self.update_user_list.append(user)
        self.update_user_list = list(set(self.update_user_list))
    
    def download_git_crypt(self) -> None:
        """Download git-crypt executable if not already present."""
        if self.git_crypt_path.exists():
            return
        
        self.log.info('Downloading git-crypt...')
        base_url = 'https://github.com/AGWA/git-crypt/releases/download/0.7.0/'
        filename = 'git-crypt-0.7.0-x86_64.exe' if platform.system().lower() == 'windows' else 'git-crypt-0.7.0-linux-x86_64'
        url = base_url + filename
        
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            with self.git_crypt_path.open('wb') as f:
                f.write(response.content)
            
            if platform.system().lower() != 'windows':
                subprocess.run(['chmod', '+x', str(self.git_crypt_path)], check=True)
            
            self.log.info('Git-crypt downloaded successfully')
        except Exception as e:
            self.log.error(f"Failed to download git-crypt: {e}")
            sys.exit(1)
    
    def get_manifest_callback(self, username: str, app_id: int, depot_id: str, manifest_gid: str, args) -> None:
        """Handle manifest retrieval callback."""
        result = args.value
        
        if not result:
            self.log.warning(f'User {username}: manifest retrieval failed - {result.code}')
            return
        
        app_path = self.ROOT / f'depots/{app_id}'
        
        try:
            delete_list = result.get('delete_list') or []
            manifest_commit = result.get('manifest_commit')
            
            if len(delete_list) > 1:
                self.log.warning(f'Multiple files deleted for {app_id}')
            
            self.set_depot_info(depot_id, manifest_gid)
            app_repo = git.Repo(app_path)
            
            with _global_lock:
                if manifest_commit:
                    app_repo.create_tag(f'{depot_id}_{manifest_gid}', manifest_commit)
                else:
                    if delete_list:
                        app_repo.git.rm(delete_list)
                    
                    app_repo.git.add(f'{depot_id}_{manifest_gid}.manifest')
                    app_repo.git.add('Key.vdf')
                    app_repo.git.add('config.json')
                    app_repo.git.add('appinfo.vdf')
                    app_repo.index.commit(f'Update depot: {depot_id}_{manifest_gid}')
                    app_repo.create_tag(f'{depot_id}_{manifest_gid}')
        except Exception as e:
            self.log.error(f"Error in manifest callback for {app_id}: {e}")
        finally:
            with _global_lock:
                if int(app_id) in self.app_lock:
                    self.app_lock[int(app_id)].discard(depot_id)
                    if int(app_id) not in self.user_info[username]['app']:
                        self.user_info[username]['app'].append(int(app_id))
                    
                    if not self.app_lock[int(app_id)]:
                        self.app_lock.pop(int(app_id))
    
    def set_depot_info(self, depot_id: str, manifest_gid: str) -> None:
        """Update depot info thread-safely."""
        with _global_lock:
            self.app_info[depot_id] = manifest_gid
    
    def save(self) -> None:
        """Save all JSON data."""
        with _global_lock:
            self.user_info.dump()
            self.app_info.dump()
    
    def get_app_worktree(self) -> Dict[str, tuple]:
        """Get list of app worktrees."""
        worktree_dict = {}
        try:
            with _global_lock:
                worktree_list = self.repo.git.worktree('list').split('\n')
            
            for worktree in worktree_list:
                parts = worktree.split()
                if len(parts) >= 3:
                    path, head, name = parts[0], parts[1], parts[2]
                    name = name.strip('()')
                    if name.isdecimal():
                        worktree_dict[name] = (path, head)
        except git.exc.GitCommandError as e:
            self.log.warning(f"Failed to get worktrees: {e}")
        
        return worktree_dict
    
    def get_remote_head(self) -> Dict[str, str]:
        """Get remote branch heads."""
        if self.remote_head:
            return self.remote_head
        
        try:
            head_dict = {}
            for line in self.repo.git.ls_remote('--head', 'origin').split('\n'):
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    commit, head = parts[0], parts[1]
                    head = head.split('/')[-1]
                    head_dict[head] = commit
            self.remote_head = head_dict
            return head_dict
        except git.exc.GitCommandError as e:
            self.log.error(f"Failed to get remote heads: {e}")
            return {}
    
    def check_app_repo_remote(self, repo: str) -> bool:
        """Check if app repo exists remotely."""
        return str(repo) in self.get_remote_head()
    
    def check_app_repo_local(self, repo: str) -> bool:
        """Check if app repo exists locally."""
        try:
            return any(branch.name == str(repo) for branch in self.repo.heads)
        except git.exc.GitCommandError:
            return False
    
    def get_remote_tags(self) -> set:
        """Fetch remote tags."""
        if self.tags:
            return self.tags
        
        try:
            for line in filter(None, self.repo.git.ls_remote('--tags').split('\n')):
                parts = line.split()
                if len(parts) >= 2:
                    tag = parts[1].split('/')[-1]
                    self.tags.add(tag)
        except git.exc.GitCommandError as e:
            self.log.warning(f"Failed to get remote tags: {e}")
        
        return self.tags
    
    def check_manifest_exist(self, depot_id: str, manifest_gid: str) -> bool:
        """Check if manifest tag exists."""
        tag_name = f'{depot_id}_{manifest_gid}'
        try:
            local_tags = {tag.name for tag in self.repo.tags}
            return tag_name in local_tags or tag_name in self.tags
        except git.exc.GitCommandError:
            return tag_name in self.tags
    
    def init_app_repo(self, app_id: int) -> None:
        """Initialize app repository worktree."""
        app_path = self.ROOT / f'depots/{app_id}'
        app_id_str = str(app_id)
        
        if app_id_str in self.get_app_worktree():
            return
        
        try:
            if app_path.exists():
                app_path.unlink()
            
            if self.check_app_repo_remote(app_id):
                with _global_lock:
                    if not self.check_app_repo_local(app_id):
                        self.repo.git.fetch('origin', f'{app_id}:origin_{app_id}')
                self.repo.git.worktree('add', '-b', app_id_str, app_path, f'origin_{app_id}')
            else:
                if self.check_app_repo_local(app_id):
                    self.log.warning(f'Branch {app_id} not found locally or remotely')
                    self.repo.git.branch('-d', app_id_str)
                self.repo.git.worktree('add', '-b', app_id_str, app_path, 'app')
        except git.exc.GitCommandError as e:
            self.log.error(f"Failed to initialize app repo {app_id}: {e}")
    
    def retry(self, func, *args, retry_num: int = -1, **kwargs) -> Any:
        """Retry a function with exponential backoff."""
        retries = retry_num if retry_num > 0 else self.retry_num
        backoff = 1
        
        while retries > 0:
            try:
                return func(*args, **kwargs)
            except gevent.timeout.Timeout as e:
                retries -= 1
                if retries > 0:
                    self.log.warning(f'Timeout, retrying in {backoff}s: {e}')
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
            except Exception as e:
                self.log.error(f"Error in retry: {e}")
                return None
        
        return None
    
    def login(self, steam: MySteamClient, username: str, password: str) -> EResult:
        """Handle user login with retry logic."""
        if self.invalid_password_count >= 15:
            self.log.error('Too many invalid password attempts. IP may be blocked.')
            return EResult.Fail
        
        self.log.info(f'Logging in as {username}...')
        shared_secret = self.two_factor.get(username)
        steam.username = username
        result = steam.relogin()
        
        if result != EResult.OK:
            result = self._perform_login(steam, username, password, shared_secret)
        
        if result == EResult.InvalidPassword:
            self.invalid_password_count += 1
        
        if result == EResult.OK:
            self.log.info(f'User {username} logged in successfully')
        else:
            self.log.error(f'Login failed for {username}: {result}')
        
        return result
    
    def _perform_login(self, steam: MySteamClient, username: str, password: str, shared_secret: Optional[str]) -> EResult:
        """Perform login attempts with rate limiting."""
        retry_count = self.retry_num
        wait_time = 1
        
        while retry_count > 0:
            two_factor = None
            if shared_secret:
                try:
                    two_factor = generate_twofactor_code(base64.b64decode(shared_secret))
                except Exception as e:
                    self.log.warning(f"Failed to generate 2FA code: {e}")
            
            result = steam.login(username, password, steam.login_key, None, two_factor_code=two_factor)
            
            if result == EResult.OK:
                return result
            
            if self.cli and result == EResult.AccountLoginDeniedNeedTwoFactor:
                try:
                    return steam.cli_login(username, password)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    break
            
            if result in (EResult.AlreadyLoggedInElsewhere, EResult.AccountDisabled, 
                         EResult.AccountLogonDenied, EResult.PasswordUnset):
                break
            
            if result == EResult.RateLimitExceeded:
                self.log.warning(f'Rate limited, waiting {wait_time}s...')
                time.sleep(wait_time)
                wait_time = min(wait_time * 2, 60)
            
            retry_count -= 1
        
        return result
    
    def async_task(self, cdn: MyCDNClient, app_id: int, appinfo: dict, package: dict, depot) -> Optional[Result]:
        """Async task for manifest retrieval."""
        try:
            self.init_app_repo(app_id)
            manifest_path = self.ROOT / f'depots/{app_id}/{depot.depot_id}_{depot.gid}.manifest'
            
            if manifest_path.exists():
                app_repo = git.Repo(self.ROOT / f'depots/{app_id}')
                try:
                    manifest_commit = app_repo.git.rev_list('-1', str(app_id),
                                                           f'{depot.depot_id}_{depot.gid}.manifest').strip()
                    return Result(result=True, app_id=app_id, depot_id=depot.depot_id, 
                                manifest_gid=depot.gid, manifest_commit=manifest_commit)
                except git.exc.GitCommandError:
                    manifest_path.unlink(missing_ok=True)
            
            return get_manifest(cdn, app_id, appinfo, package, depot, True, self.ROOT, self.retry_num)
        except Exception as e:
            self.log.error(f"Error in async_task for app {app_id}: {e}")
            return None
    
    def get_manifest(self, username: str, password: str, sentry_name: Optional[str] = None) -> None:
        """Main manifest retrieval for a user."""
        self.users += 1
        total_users = len(self.update_user_list) or len(self.user_info)
        self.log.info(f'Processing user {self.users}/{total_users}: {username}')
        
        # Initialize user info
        if username not in self.user_info:
            self.user_info[username] = {'app': [], 'update': 0, 'enable': True}
        
        user_info = self.user_info[username]
        user_info.setdefault('enable', True)
        user_info.setdefault('app', [])
        user_info.setdefault('update', 0)
        
        # Check if user is enabled
        if not user_info['enable']:
            self.log.warning(f'User {username} is disabled')
            return
        
        # Check update interval
        time_since_update = time.time() - user_info['update']
        if time_since_update < self.update_wait_time:
            wait_time = int(self.update_wait_time - time_since_update)
            self.log.warning(f'User {username} will be updated in {wait_time}s')
            return
        
        try:
            # Setup sentry path
            sentry_path = None
            if sentry_name:
                sentry_path = (self.credential_location if self.credential_location 
                             else MySteamClient.credential_location) / sentry_name
            
            # Login
            steam = MySteamClient(str(self.credential_location), sentry_path)
            result = self.login(steam, username, password)
            if result != EResult.OK:
                return
            
            # Initialize CDN client
            self.log.info(f'Initializing CDN client for {username}...')
            cdn = self.retry(MyCDNClient, steam, retry_num=self.retry_num)
            if not cdn:
                self.log.error(f'Failed to initialize CDN for {username}')
                return
            
            # Load licenses
            app_id_list = cdn.load_licenses()
            self.log.info(f'User {username}: {len(app_id_list)} apps found')
            
            if not app_id_list:
                user_info['enable'] = False
                self.log.warning(f'User {username} has no apps')
                return
            
            # Get app info
            self.log.info(f'Fetching app info for {username}...')
            fresh_resp = self.retry(steam.get_product_info, app_id_list, timeout=30)
            if not fresh_resp:
                self.log.error(f'Failed to get app info for {username}')
                return
            
            # Process manifests
            self._process_manifests(username, app_id_list, fresh_resp, cdn, steam)
            
            # Update last update time
            with _global_lock:
                user_info['update'] = int(time.time())
        
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self.log.error(f"Error processing {username}: {e}")
    
    def _process_manifests(self, username: str, app_id_list: List[int], fresh_resp: dict, 
                          cdn: MyCDNClient, steam: MySteamClient) -> None:
        """Process manifests for all apps."""
        job_list = []
        has_new_manifest = False
        
        for app_id in app_id_list:
            if self.update_app_id_list and int(app_id) not in self.update_app_id_list:
                continue
            
            with _global_lock:
                if int(app_id) in self.app_lock:
                    continue
                self.app_lock[int(app_id)] = set()
            
            try:
                manifests = cdn.get_manifests(int(app_id))
                if not manifests:
                    continue
                
                app = fresh_resp['apps'].get(app_id, {})
                package = self._get_package_info(app, manifests, steam)
                
                for depot in manifests:
                    depot_id = str(depot.depot_id)
                    manifest_gid = str(depot.gid)
                    
                    self.app_lock[int(app_id)].add(depot_id)
                    self.set_depot_info(depot_id, manifest_gid)
                    
                    with _global_lock:
                        if int(app_id) not in self.user_info[username]['app']:
                            self.user_info[username]['app'].append(int(app_id))
                        
                        if self.check_manifest_exist(depot_id, manifest_gid):
                            continue
                    
                    has_new_manifest = True
                    job = gevent.Greenlet(SafeExceptionHandler(self.async_task), 
                                        cdn, app_id, app, package, depot)
                    job.rawlink(functools.partial(self.get_manifest_callback, username, app_id, depot_id, manifest_gid))
                    job_list.append(job)
                    gevent.idle()
                
                # Start jobs for this app
                for job in job_list[-len(manifests):]:
                    job.start()
            
            except Exception as e:
                self.log.error(f"Error processing app {app_id}: {e}")
            finally:
                with _global_lock:
                    if int(app_id) in self.app_lock and not self.app_lock[int(app_id)]:
                        self.app_lock.pop(int(app_id))
        
        # Wait for all jobs to complete
        if job_list:
            gevent.joinall(job_list)
    
    def _get_package_info(self, app: dict, manifests: list, steam: MySteamClient) -> dict:
        """Extract DLC and package information."""
        package = {'dlcs': [], 'packagedlcs': []}
        
        try:
            if 'extended' not in app or 'listofdlc' not in app['extended']:
                return package
            
            dlc_list = list(map(int, app['extended']['listofdlc'].split(',')))
            package['dlcs'] = dlc_list
            
            dlc_info = self.retry(steam.get_product_info, dlc_list, timeout=30)
            if dlc_info:
                for appid, info in dlc_info.get('apps', {}).items():
                    if info.get('depots'):
                        package['packagedlcs'].append(int(appid))
                        package['dlcs'].remove(int(appid))
            
            # Remove depots already in manifests
            manifest_depot_ids = {str(depot.depot_id) for depot in manifests}
            for depotid, info in app.get('depots', {}).items():
                if 'dlcappid' in info and 'manifests' in info:
                    dlc_appid = int(info['dlcappid'])
                    if dlc_appid in package['dlcs'] and depotid in manifest_depot_ids:
                        package['dlcs'].remove(dlc_appid)
        
        except Exception as e:
            self.log.warning(f"Error getting package info: {e}")
        
        return package
    
    def run(self, update: bool = False) -> None:
        """Main execution method."""
        if not self.account_info or self.init_only:
            self.log.info('Initialization complete')
            self.save()
            return
        
        if update:
            self.update()
            if not self.update_user_list:
                return
        
        # Process users with thread pool
        with Pool(self.pool_num) as pool:
            result_list = []
            for username in self.account_info:
                if self.update_user_list and username not in self.update_user_list:
                    continue
                
                password, sentry_name = self.account_info[username]
                result_list.append(
                    pool.apply_async(SafeExceptionHandler(self.get_manifest), 
                                   (username, password, sentry_name))
                )
            
            try:
                # Wait for results
                start_time = time.time()
                while any(not result.ready() for result in result_list):
                    if time.time() - start_time > 3600:  # 1 hour timeout
                        self.log.warning('Processing timeout reached')
                        break
                    self.save()
                    time.sleep(5)
                
                self.log.info('All users processed successfully')
                time.sleep(10)
            
            except KeyboardInterrupt:
                self.log.info('Interrupted by user')
                pool.terminate()
                sys.exit(0)
            
            finally:
                self.save()
    
    def update(self) -> None:
        """Check for app updates and determine which users need updating."""
        self.log.info('Checking for app updates...')
        
        # Collect all app IDs
        app_id_list = []
        for user, info in self.user_info.items():
            if info.get('enable') and info.get('app'):
                app_id_list.extend(info['app'])
        
        app_id_list = list(set(app_id_list))
        if not app_id_list:
            self.log.info('No apps to check for updates')
            return
        
        # Anonymous login and fetch app info
        try:
            steam = MySteamClient(str(self.credential_location))
            self.log.info('Logging in anonymously...')
            steam.anonymous_login()
            
            # Fetch app info in batches
            app_info_dict = {}
            for i in range(0, len(app_id_list), 300):
                batch = app_id_list[i:i+300]
                fresh_resp = self.retry(steam.get_product_info, batch, timeout=60)
                if fresh_resp:
                    for app_id, info in fresh_resp['apps'].items():
                        if depots := info.get('depots'):
                            app_info_dict[int(app_id)] = depots
            
            # Find updated apps
            update_app_set = set()
            for app_id, app_depots in app_info_dict.items():
                for depot_id, depot in app_depots.items():
                    if depot_id.isdecimal() and (manifests := depot.get('manifests')):
                        if manifest := manifests.get('public'):
                            if depot_id in self.app_info and self.app_info[depot_id] != manifest:
                                update_app_set.add(app_id)
            
            # Find users to update
            update_user_set = set()
            for user, info in self.user_info.items():
                if info.get('enable') and info.get('app'):
                    if any(app_id in update_app_set for app_id in info['app']):
                        update_user_set.add(user)
            
            # Add users without info
            for user in self.account_info:
                if user not in self.user_info:
                    update_user_set.add(user)
            
            self.update_user_list.extend(list(update_user_set))
            self.log.info(f'{len(update_app_set)} apps and {len(update_user_set)} users need updates')
        
        except Exception as e:
            self.log.error(f"Update check failed: {e}")


def main():
    """Entry point."""
    args = parser.parse_args()
    
    try:
        updater = ManifestAutoUpdate(
            credential_location=args.credential_location,
            level=args.level,
            pool_num=args.pool_num,
            retry_num=args.retry_num,
            update_wait_time=args.update_wait_time,
            key=args.key,
            init_only=args.init_only,
            cli=args.cli,
            app_id_list=args.app_id_list,
            user_list=args.user_list,
        )
        
        updater.run(update=args.update)
        
        if not args.no_push and not args.init_only:
            push()
            push_data()
    
    except KeyboardInterrupt:
        logging.info('Interrupted by user')
        sys.exit(0)
    except Exception as e:
        logging.error(f'Fatal error: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
                
