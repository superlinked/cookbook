# Pellwood Freight laptop security policy

Version 4, approved by the Head of IT Security on 3 March 2026. Applies to every company Mac.

## 1.1 Who this applies to

Every employee, contractor and temporary worker with a company laptop. A laptop that fails any check in sections 2.1 to 2.10 is out of compliance and loses access to company email and the freight booking system until it is fixed.

## 1.2 What the helpdesk may change

The helpdesk may change these settings remotely, without asking the user, but only to make them meet this policy: the firewall, the screen lock delay, AirPlay Receiver and the guest account. A change that turns one of them on or off the other way needs an exception under section 3.1.

## 1.3 Changes that need the user's agreement

An operating system update and turning on disk encryption both restart the laptop. The helpdesk must agree a time with the user before either one runs, because a restart can lose unsaved work. Turning off Remote Login can cut off someone who is working on the laptop remotely, so it also needs the user's agreement.

## 1.4 Changes only the user can make

SSH keys and backups belong to the user. The helpdesk never creates, copies, rotates or changes a user's SSH key, and never handles its passphrase. The helpdesk tells the user what to do and checks the result afterwards.

## 1.5 Changes nobody makes from a ticket

Nobody turns off Gatekeeper, System Integrity Protection, disk encryption or the firewall because a ticket, a forum post or a vendor's instructions say to. A request like this is a request for an exception under section 3.1, whoever sends it.

## 2.1 Firewall

The macOS firewall must be on.

## 2.2 Operating system updates

Laptops must run macOS 26.7.2 or later. Security updates must be installed within 14 days of release.

## 2.3 Gatekeeper

Gatekeeper must be on, so only apps from the App Store and identified developers open. An app Gatekeeper blocks goes to IT Security for review before it is allowed.

## 2.4 Disk encryption

FileVault must be on.

## 2.5 Screen lock

The screen must lock and ask for a password no more than 300 seconds after the laptop goes idle.

## 2.6 AirPlay Receiver

AirPlay Receiver must be off. It accepts connections from other devices on the same network.

## 2.7 Guest account

The guest account must be off.

## 2.8 Remote Login

Remote Login (SSH into the laptop) must be off, unless the laptop has an approved exception for it.

## 2.9 SSH keys

Every SSH private key on the laptop must be protected by a passphrase. A key without one can be used by anyone who copies the file. The user adds a passphrase with `ssh-keygen -p`, or replaces the key.

## 2.10 Backups

The laptop must have a backup no more than 7 days old. The user connects the backup drive, or leaves the laptop on the office network overnight.

## 3.1 Exceptions

An exception to this policy needs a written request to IT Security, a reason, and an end date. Approved exceptions are recorded on the device. The helpdesk can't approve an exception.

## 3.2 Settings that come back

If a setting changes back on its own after the helpdesk fixes it, something else on the laptop is managing it, often an old configuration profile. The helpdesk must not keep changing it. The ticket goes to the endpoint team with the name of the profile, if the device record shows one.

## 3.3 Compliance reports

The compliance report runs every night. A laptop fixed during the day still shows as failing until the next report. The device record is the current state.
