import os
import json
import base64
import re
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from colorama import init, Fore
import yaml
import requests
from prompt import planning_agent_prompt, parse_email_command_prompt, scheduling_agent_prompt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import base64
import pytz

# Initialize colorama and auto-reset color after each print
init(autoreset=True)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar"
]

def get_service(api_name, api_version):
    """Authenticate and return the specified Google service."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(Fore.RED + f" Token Refresh failed:{e}")
                os.remove("token.json")
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    service = build(api_name, api_version, credentials=creds)
    return service

def load_config(file_path):
    """Load configuration from a YAML file and set environment variables."""
    with open(file_path, "r") as file:
        config = yaml.safe_load(file)
    for key, value in config.items():
        os.environ[key] = value
    print("Environment variables loaded:", {k: os.getenv(k) for k in config.keys()})

def load_tasks(file_path="plan.json"):
    """Load tasks from a JSON file."""
    if os.path.exists(file_path):
        with open(file_path, "r") as file:
            content = file.read().strip()
            if content:
                return json.loads(content)
    return []

def save_tasks(tasks, file_path="plan.json"):
    with open(file_path, "w") as file:
        json.dump(tasks, file, indent=4)


def parse_due_date(due_date_str):   
    match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", due_date_str)
    if match:
        due_date_str = match.group(0)
    else:
        return None
    date_formats = [
        "%Y-%m-%d %H:%M:%S",  # Full date with time
        "%Y-%m-%d",           # Date only
        "%I:%M %p"            # Time only
    ]
    for fmt in date_formats:
        try:
            return datetime.strptime(due_date_str, fmt)
        except ValueError:
            continue
    return None  # Invalid date format

class Agent:
    def __init__(self, model, temperature=0, max_tokens=1000, verbose=False):
        load_config("config.yaml")
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.url = "https://api.openai.com/v1/chat/completions"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if model is None:
            raise ValueError("The model parameter must be provided.")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.planning_agent_prompt = planning_agent_prompt
        self.parse_email_command_prompt = parse_email_command_prompt
        self.scheduling_agent_prompt = scheduling_agent_prompt
        self.model = model
        self.verbose = verbose

    def run_planning_agent(self, query, current_tasks):
        system_prompt = self.planning_agent_prompt.format(
            user_input=query, current_tasks=json.dumps(current_tasks)
        )

        data = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": query},
                {"role": "system", "content": system_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        response = requests.post(self.url, headers=self.headers, json=data, timeout=180)
        response_dict = response.json()

        if self.verbose:
            print(Fore.YELLOW + f"Response from OpenAI API: {response_dict}")

        if "choices" in response_dict and response_dict["choices"]:
            content = response_dict["choices"][0]["message"]["content"]
            print(Fore.GREEN + f"Planning Agent: {content}")
            return content
        else:
            print(Fore.RED + "Error: 'choices' not found in the response.")
            return None
        
    def run_scheduling_agent(self, task_description):
        """Run the scheduling agent to get an exact due date."""
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system_prompt = self.scheduling_agent_prompt.format(task_description=task_description, current_date = current_date)

        data = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": task_description},
                {"role": "system", "content": system_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        response = requests.post(self.url, headers=self.headers, json=data, timeout=180)
        response_dict = response.json()

        if self.verbose:
            print(Fore.YELLOW + f"Response from OpenAI API: {response_dict}")

        if "choices" in response_dict and response_dict["choices"]:
            content = response_dict["choices"][0]["message"]["content"]
            print(Fore.GREEN + f"Scheduling Agent: {content}")
            return content.strip()
        else:
            print(Fore.RED + "Error: 'choices' not found in the response.")
            return None

    def extract_task_from_plan(self, plan):
        """Extract the task description and due date from the full plan."""
        print(Fore.BLUE + f"Plan received for extraction: {plan}")
        task_description = None
        due_date = None

        if "Task added:" in plan:
            task_description = plan.split("Task added:")[1].strip()
        elif "Task:" in plan:
            task_description = plan.split("Task:")[1].strip()
        elif "Action:" in plan:
            task_description = plan.split("Action:")[1].strip()

        if task_description:
            due_date_match = re.search(r"Due:\s*(.*)", task_description)
            if due_date_match:
                due_date_str = due_date_match.group(1).strip()
                due_date = parse_due_date(due_date_str)
                task_description = re.sub(r"Due:\s*.*", "", task_description).strip()
            if due_date is None or due_date_str == "Not specified":
                due_date_str = self.run_scheduling_agent(task_description)
                due_date = parse_due_date(due_date_str)
                while due_date is None:
                    print(Fore.YELLOW + "Due date is not provided. Please provide a specific due date or time for the task.")
                    due_date_str = input("Enter due date (format: YYYY-MM-DD HH:MM:SS or relative terms like 'tomorrow'): ")
                    due_date_str = self.run_scheduling_agent(due_date_str)
                    due_date = parse_due_date(due_date_str)
                    if due_date is None:
                        print(Fore.RED + "Invalid date format. Please try again.")      

            if isinstance(due_date, datetime):
                due_date = due_date.strftime("%Y-%m-%d %H:%M:%S")
            else:
                due_date = "Not specified"

        return {"task": task_description, "due_date": due_date} if task_description else None

    def parse_email_command(self, query):
        system_prompt = self.parse_email_command_prompt.format(user_input=query)

        data = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": query},
                {"role": "system", "content": system_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            response = requests.post(self.url, headers=self.headers, json=data, timeout=180)
            response_dict = response.json()

            if self.verbose:
                print(Fore.YELLOW + f"Response from OpenAI API: {response_dict}")

            if "choices" in response_dict and response_dict["choices"]:
                content = response_dict["choices"][0]["message"]["content"]
                try:
                    result = json.loads(content)
                    return result.get("send_email", False), result.get("email_command")
                except (ValueError, KeyError):
                    print(Fore.RED + "Error: Invalid response format from the AI agent.")
                    return False, None
            else:
                print(Fore.RED + "Error: 'choices' not found in the response.")
                return False, None
        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Request failed: {e}")
            return False, None

    def create_calendar_event(self, task):
        service = get_service("calendar", "v3")
        task_description = task["task"]
        due_date_str = task["due_date"]
        due_date = parse_due_date(due_date_str)

        if not due_date or due_date_str == "Not specified":
            print(Fore.YELLOW + "Due date not specified or invalid. Skipping calendar event creation.")
            return
        
        ist = pytz.timezone('Asia/Kolkata')
        due_date_ist = due_date.astimezone(ist)
        start_time = (due_date_ist - timedelta(minutes=30)).isoformat()
        end_time = due_date_ist.isoformat()

        event = {
            'summary': task_description,
            'start': {'dateTime': start_time, 'timeZone': 'UTC'},
            'end': {'dateTime': end_time, 'timeZone': 'UTC'},
        }
        try:
            event = service.events().insert(calendarId='primary', body=event).execute()
            print(Fore.GREEN + f"Event created: {event.get('htmlLink')}")
        except Exception as e:
            print(Fore.RED + f"failed to create calender event: {e}")

    def execute(self, iterations=5):
        query = input("Enter your query: ")
        tasks = load_tasks()
        meets_requirements = False
        iterations_count = 0

        # Use the AI agent to determine if the user wants to send tasks to email
        send_email, email_command = self.parse_email_command(query)

        while not meets_requirements and not send_email and iterations_count < iterations:
            iterations_count += 1
            plan = self.run_planning_agent(query, tasks)

            if plan:
                task_info = self.extract_task_from_plan(plan)
                if task_info:  # Only proceed if a valid task info is returned
                    tasks.append(task_info)
                    save_tasks(tasks)
                    print(Fore.CYAN + f"Final Response: {task_info['task']} (Due: {task_info['due_date']})")
                    self.create_calendar_event(task_info)  # Create a calendar event for the task
                    meets_requirements = True
                else:
                    print(Fore.RED + "No new task was added.")
            if iterations_count >= iterations:
                print(Fore.YELLOW + "No more tasks to add. Exiting loop.")
                break

        if send_email:
            if tasks:  # Check if there are tasks to send
                email_subject = "Your Todo Tasks"
                self.send_tasks_summary_to_email(email_subject, tasks)
                print(Fore.GREEN + f"Tasks sent to email using the command: {email_command}")
            else:
                print(Fore.YELLOW + "No tasks available to send to email.")

    def send_tasks_summary_to_email(self, subject, tasks):       
        service = get_service("gmail", "v1")
        email_subject = "My Todo Tasks"
    
        task_summaries = []
        for i, task in enumerate(tasks):
            # Extract task description and due date
            task_desc = re.search(r"^(.*?)(\n|$)", task['task']).group(1)
            due_date = task['due_date']
        
            # Format task summary
            task_summary = f"{i+1}) {task_desc} (Due: {due_date})"
            task_summaries.append(task_summary)
            
        task_list = "\n".join(task_summaries)
    # Create the email content
        email_content = f"Subject: {email_subject}\n\nHere are your tasks:\n\n{task_list}"
    
        recipient_email = os.getenv("RECIPIENT_EMAIL")
        if not recipient_email:
            print(Fore.RED + "Recipient email address is not set. Please set the RECIPIENT_EMAIL environment variable.")
            return
    
        message = MIMEText(email_content)
        message['to'] = recipient_email
        message['from'] = "me"  # Assuming the sender is authenticated user
        message['subject'] = email_subject
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
    
        try:
            message = service.users().messages().send(userId="me", body={'raw': raw_message}).execute()
            print(Fore.GREEN + f"Email sent: {message['id']}")
        except Exception as e:
            print(Fore.RED + f"Failed to send email: {e}")

if __name__ == "__main__":
    model = "gpt-3.5-turbo"
    temperature = 0.7
    max_tokens = 1500
    verbose = True
    agent = Agent(model, temperature, max_tokens, verbose)
    agent.execute()
