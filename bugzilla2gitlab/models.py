import re
import dateutil.parser as dateparser

from .utils import _perform_request, markdown_table_row, format_datetime

conf = None


class IssueThread(object):
    '''
    Everything related to an issue in GitLab, e.g. the issue itself and subsequent comments.
    '''
    def __init__(self, config, fields):
        global conf
        conf = config
        self.load_objects(fields)

    def load_objects(self, fields):
        '''
        Load the issue object and the comment objects.
        If conf.dry_run=False, then Attachments are created in GitLab in this step.
        '''
        self.issue = Issue(fields)
        self.comments = []
        '''
        fields["long_desc"] gets peared down in Issue creation (above). This is because bugzilla
        lacks the concept of an issue description, so the first comment is harvested for
        the issue description, as well as any subsequent comments that are simply attachments
        from the original reporter. What remains below should be a list of genuine comments.
        '''

        for comment_fields in fields["long_desc"]:
            if comment_fields.get("thetext"):
                self.comments.append(Comment(comment_fields, self.issue.attachments))

    def save(self):
        '''
        Save the issue and all of the comments to GitLab.
        If conf.dry_run=True, then only the HTTP request that would be made is printed.
        '''
        self.issue.save()

        for comment in self.comments:
            comment.issue_id = self.issue.iid
            comment.save()

        # close the issue in GitLab, if it is resolved in Bugzilla
        if self.issue.status == "RESOLVED":
            self.issue.close()


class Issue(object):
    '''
    The issue model
    '''
    required_fields = ["title", "description", "status"]
    data_fields = ["title", "description", "status", "assignee_id", "milestone",
                   "labels", "created_at"]

    def __init__(self, bugzilla_fields):
        self.headers = conf.default_headers.copy()
        validate_user(bugzilla_fields["reporter"])
        validate_user(bugzilla_fields["assigned_to"])
        self.load_fields(bugzilla_fields)

    def load_fields(self, fields):
        self.created_at = dateparser.parse (fields["creation_ts"]).isoformat()
        self.title = fields["short_desc"]
        self.sudo = conf.gitlab_users[conf.bugzilla_users[fields["reporter"]]]
        self.assignee_id = conf.gitlab_users[conf.bugzilla_users[fields["assigned_to"]]]
        self.status = fields["bug_status"]
        self.create_labels(fields["component"], fields.get("op_sys"))
        self.attachments = self.create_description(fields)

    def create_labels(self, component, operating_system):
        '''
        Creates 3 types of labels: default labels listed in the configuration, component labels,
        and operating system labels.
        '''
        labels = []
        if conf.default_gitlab_labels:
            labels.extend(conf.default_gitlab_labels)

        component_label = conf.component_mappings.get(component)
        if component_label:
            labels.append(component_label)

        # Do not create a label if the OS is other. That is a meaningless label.
        if operating_system and operating_system != "Other":
            labels.append(operating_system)

        self.labels = ",".join(labels)

    def create_description(self, fields):
        '''
        An opinionated description body creator.
        '''
        ext_description = ""

        # markdown table header
        self.description = markdown_table_row("", "")
        self.description += markdown_table_row("---", "---")

        if conf.include_bugzilla_link:
            bug_id = fields["bug_id"]
            link = "{}/show_bug.cgi?id={}".format(conf.bugzilla_base_url, bug_id)
            self.description += markdown_table_row("Bugzilla Link",
                                                   "[{}]({})".format(bug_id, link))

        formatted_dt = format_datetime(fields["creation_ts"], conf.datetime_format_string)
        self.description += markdown_table_row("Created on", formatted_dt)

        if fields.get("resolution"):
            self.description += markdown_table_row("Resolution", fields["resolution"])
            self.description += markdown_table_row("Resolved on",
                                                   format_datetime(fields["delta_ts"],
                                                                   conf.datetime_format_string))

        self.description += markdown_table_row("Version", fields.get("version"))
        self.description += markdown_table_row("OS", fields.get("op_sys"))
        self.description += markdown_table_row("Architecture", fields.get("rep_platform"))

        # add first comment to the issue description
        if (fields["reporter"] == fields["long_desc"][0]["who"] and
                fields["long_desc"][0]["thetext"]):
            ext_description += "\n## Extended Description \n"
            ext_description += "\n\n".join(re.split("\n*", needs_quoting(fields["long_desc"][0]["thetext"])))
            del fields["long_desc"][0]

        attachments = dict()
        attachment_files = list()
        obsolete_attachments = list()
        for i in range(0, len(fields["attachment"])):
            attachment = fields["attachment"][i]
            if not attachment["isobsolete"]:
                # check if we have a duplicate filenames and compensate
                matchs = [x for x in attachment_files if attachment["filename"] in x]
                if matchs and len(matchs) < 2:
                    attachment["filename"] = "x" + matchs[0]
                    attachment_files.remove(matchs[0])
                    attachment_files.append(attachment["filename"])
                else:
                    attachment_files.append(attachment["filename"])
                attachments[int(attachment["attachid"])] = Attachment(int(attachment["attachid"]), attachment["filename"])
            else:
                obsolete_attachments.append(int(attachment["attachid"]))

        to_delete = []
        for i in range(0, len(fields["long_desc"])):
            comment = fields["long_desc"][i]
            # any attachments from the reporter in comments should also go in the issue description
            attachment_id = Attachment.parse_comment_text(comment.get("thetext"))
            if attachment_id:
                if comment.get("who") == fields["reporter"]:
                    if attachment_id in attachments or attachment_id in obsolete_attachments:
                        to_delete.append(i)
                    else:
                        raise Exception("Match `{}' found in comment `{}', but attachment does not exist!".format(attachment_id, comment.get("thetext")))
                else: # when another user attaches something
                    if attachment_id in attachments or attachment_id in obsolete_attachments:
                        # This might result in an empty comment, which is ignored by the API
                        regex = "Created.*\(id=\d+\)(?:.*\n*){1,2}"
                        txt = comment.get("thetext")
                        rep = re.sub(regex, "", txt, count=1)
                        fields["long_desc"][i]["thetext"] = rep
                        fields["long_desc"][i]["attachid"] = attachment_id
                    else:
                        raise Exception("Match `{}' found in comment `{}', but attachment does not exist!".format(attachment_id, comment.get("thetext")))

        # delete comments that have already added to the issue description
        for i in reversed(to_delete):
            del fields["long_desc"][i]

        if attachments:
            attachment_markdown = []
            for key, attachment in attachments.items():
                attachment_markdown.append(str(attachment))
            self.description += markdown_table_row("Attachments", ", ".join(attachment_markdown))

        if ext_description:
            # for situations where the reporter is a generic or old user, specify the original
            # reporter in the description body
            if fields["reporter"] == conf.bugzilla_auto_reporter:
                # try to get reporter email from the body
                description, part, user_data = ext_description.rpartition("Submitter was ")
                # partition found matching string
                if part:
                    regex = r"^(\S*)\s?.*$"
                    email = re.match(regex, user_data, flags=re.M).group(1)
                    self.description += markdown_table_row("Reporter", email)
            # Add original reporter to the markdown table
            elif conf.bugzilla_users[fields["reporter"]] == conf.gitlab_misc_user:
                self.description += markdown_table_row("Reporter", fields["reporter"])

            self.description += ext_description

        return attachments

    def validate(self):
        for field in self.required_fields:
            value = getattr(self, field)
            if not value:
                raise Exception("Missing value for required field: {}".format(field))
        return True

    def save(self):
        self.validate()
        if not conf.gitlab_user_admins[self.sudo]:
            set_user_isadmin(self.headers["private-token"], self.sudo, "true")

        url = "{}/projects/{}/issues".format(conf.gitlab_base_url, conf.gitlab_project_id)
        data = {k: v for k, v in self.__dict__.items() if k in self.data_fields}
        self.headers["sudo"] = self.sudo

        response = _perform_request(url, "post", headers=self.headers, data=data, json=True,
                                    dry_run=conf.dry_run)

        if not conf.gitlab_user_admins[self.sudo]:
            set_user_isadmin(self.headers["private-token"], self.sudo, "false")

        if conf.dry_run:
            # assign a random number so that program can continue
            self.id = 5
            self.iid = 50
            return

        self.iid = response["iid"]
        self.id = response["id"]


    def close(self):
        url = "{}/projects/{}/issues/{}".format(conf.gitlab_base_url, conf.gitlab_project_id,
                                                self.iid)
        data = {
            "state_event": "close",
        }
        self.headers["sudo"] = self.sudo

        _perform_request(url, "put", headers=self.headers, data=data, dry_run=conf.dry_run)


class Comment(object):
    '''
    The comment model
    '''

    required_fields = ["body", "issue_id"]
    data_fields = ["body", "created_at"]

    def __init__(self, bugzilla_fields, attachments):
        self.headers = conf.default_headers.copy()
        validate_user(bugzilla_fields["who"])
        self.attachments = attachments
        self.load_fields(bugzilla_fields)

    def load_fields(self, fields):
        self.sudo = conf.gitlab_users[conf.bugzilla_users[fields["who"]]]
        # if unable to comment as the original user, put username in comment body
        if conf.bugzilla_users[fields["who"]] == conf.gitlab_misc_user:
            self.body = "By {}\n\n".format(fields["who"])
        else:
            self.body = ""

        # if this comment is actually an attachment, upload the attachment and add the
        # markdown to the comment body
        self.created_at = dateparser.parse (fields["bug_when"]).isoformat()
        if fields.get("attachid"):
            attachment_id = fields.get("attachid")
            attachment_markdown = str(self.attachments[attachment_id])
            self.body += attachment_markdown + "\n"

        self.body += needs_quoting(fields["thetext"])

    def validate(self):
        for field in self.required_fields:
            value = getattr(self, field)
            if not value:
                raise Exception("Missing value for required field: {}".format(field))

    def save(self):
        self.validate()
        if not conf.gitlab_user_admins[self.sudo]:
            set_user_isadmin(self.headers["private-token"], self.sudo, "true")
        url = "{}/projects/{}/issues/{}/notes".format(conf.gitlab_base_url, conf.gitlab_project_id,
                                                      self.issue_id)
        data = {k: v for k, v in self.__dict__.items() if k in self.data_fields}
        self.headers["sudo"] = self.sudo

        _perform_request(url, "post", headers=self.headers, data=data, json=True,
                         dry_run=conf.dry_run)

        if not conf.gitlab_user_admins[self.sudo]:
            set_user_isadmin(self.headers["private-token"], self.sudo, "false")

class Attachment(object):
    '''
    The attachment model
    '''
    def __init__(self, bugzilla_attachment_id, filename):
        self.id = bugzilla_attachment_id
        self.filename = filename
        self.headers = conf.default_headers.copy()
        self.markdown = self.__upload_attachment()

    def __str__(self):
        return self.markdown

    @classmethod
    def parse_comment_text(cls, comment):
        regex = "^Created an attachment \(id=(\d+)\).*$"
        matches = re.match(regex, comment, flags=re.M)
        if not matches:
            return False
        return int(matches.group(1))

    def __upload_attachment(self):
        url = "{}/attachment.cgi?id={}".format(conf.bugzilla_base_url, self.id)
        result = _perform_request(url, "get", json=False)

        url = "{}/projects/{}/uploads".format(conf.gitlab_base_url, conf.gitlab_project_id)
        f = {"file": (self.filename, result.content)}

        attachment = _perform_request(url, "post", headers=self.headers, files=f, json=True,
                                      dry_run=conf.dry_run)

        if conf.dry_run:
            return "[attachment]({})".format(self.filename)

        return attachment["markdown"]


def validate_user(bugzilla_user):
    if bugzilla_user not in conf.bugzilla_users:
        raise Exception("Bugzilla user `{}` not found in user_mappings.yml. "
                        "Please add them before continuing.".format(bugzilla_user))

def set_user_isadmin(private_token, user_id, state):
    header = {"private-token": private_token,}
    url = "{}/users/{}".format(conf.gitlab_base_url, user_id)
    data = {"admin": state}
    response = _perform_request(url, "put", data=data, headers=header)

    return response

def needs_quoting (txt):
    if "#" in txt or \
       ">" in txt or \
       "_" in txt or \
       "-" in txt or \
       txt.count("=") > 1 or \
       "~" in txt or \
       "|" in txt or \
       "*" in txt or \
       "+" in txt or \
       "@" in txt:
        return "<pre>%s</pre>" % txt

    return txt
