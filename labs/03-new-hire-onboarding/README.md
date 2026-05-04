# Lab 03 – New Hire Onboarding Packet

Event-driven onboarding automation: when HR adds an employee to a SharePoint
"New Hires" list, the app automatically provisions resources and sends notifications.

```
SharePoint List (new item) → Gateway Trigger → Sandbox (Flask handler)
                                                  ├── Create OneDrive folder
                                                  ├── Send welcome email to new hire
                                                  └── Send heads-up email to manager
```

## What You'll Build

1. **SharePoint trigger** — fires when a new item is added to the "New Hires" list
2. **Onboarding handler** — Flask app in a sandbox that:
   - Reads the new hire record (name, role, start date, manager email)
   - Creates a OneDrive folder `/Onboarding/{Name}` with welcome docs
   - Sends a welcome email to the new hire with links + first-day instructions
   - Sends the manager a heads-up email

## Prerequisites

- Azure CLI [signed in](https://learn.microsoft.com/cli/azure/authenticate-azure-cli-interactively)
- SDKs: `pip install azure-connectorgateway azure-sandbox azure-mgmt-sandbox`
- A SharePoint site with a "New Hires" list (columns: Title, Role, StartDate, ManagerEmail, EmployeeEmail)

## Run

Open `01-new-hire-onboarding.ipynb` and click **Run All** or step through each cell.
