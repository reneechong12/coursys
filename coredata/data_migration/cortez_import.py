import sys, os
sys.path.append(".")
sys.path.append("..")
os.environ['DJANGO_SETTINGS_MODULE'] = 'settings'
from django.db import transaction
from django.db.utils import IntegrityError
from django.db.models import Max
from coredata.queries import DBConn, get_names, get_or_create_semester, add_person, get_person_by_userid
from coredata.models import Person, Semester, Unit, CourseOffering, Course, SemesterWeek
from grad.models import GradProgram, GradStudent, GradRequirement, CompletedRequirement, Supervisor, GradStatus, \
        Letter, LetterTemplate, Promise, Scholarship, ScholarshipType, OtherFunding, GradProgramHistory, GradFlag, \
        FinancialComment
from ta.models import TAContract, TAApplication, TAPosting, TACourse, CoursePreference, SkillLevel, Skill, CourseDescription, CampusPreference
from ra.models import RAAppointment, Account, Project
from coredata.importer import AMAINTConn, get_person, get_person_grad, import_one_offering, import_instructors, update_amaint_userids
import datetime, json, time, decimal

import pymssql, MySQLdb
CORTEZ_USER = 'ggbaker'

# with the Ubuntu package python-pymssql, this change must be made
# in /etc/freetds/freetds.conf: [http://www.freetds.org/userguide/choosingtdsprotocol.htm]
# [global]
#    tds version = 7.0
# or run with
#   TDSVER=7.0 python coredata/cortez_import.py

# https://bugs.launchpad.net/ubuntu/+source/pymssql/+bug/918896

# needs these ports forwarded to the databases:
#   ssh -L 1433:cortez.cs.sfu.ca:1433 oak.fas.sfu.ca # cortez DB
#   ssh -L 4000:localhost:4000 -L 50000:localhost:50000 courses.cs.sfu.ca # AMAINT and SIMS

# ln -s /usr/lib/pyshared/python2.7/pymssql.so ../lib/python2.7/site-packages/
# ln -s /usr/lib/pyshared/python2.7/_mssql.so ../lib/python2.7/site-packages/


# update weird value from test data
Unit.objects.filter(slug='comp').update(label='CMPT', slug='cmpt')

class CortezConn(DBConn):
    db_host = '127.0.0.1'
    db_user = "fas.sfu.ca\\" + CORTEZ_USER
    db_name = "ra"
    def escape_arg(self, a):
        return "'" + MySQLdb.escape_string(str(a)) + "'"
    def prep_value(self, v):
        if isinstance(v, basestring):
            return v.strip()
        else:
            return v

    def get_connection(self):
        passfile = open(self.dbpass_file)
        _ = passfile.next()
        _ = passfile.next()
        _ = passfile.next()
        pw = passfile.next().strip()

        conn = pymssql.connect(host=self.db_host, user=self.db_user,
             password=pw, database=self.db_name)
        return conn, conn.cursor()

class Introspection(object):
    def table_columns(self, dbname, table):
        db = CortezConn()
        db.execute("USE ["+dbname+"]", ())
        db.execute("select column_name, data_type, is_nullable, character_maximum_length from information_schema.columns where table_name=%s order by ordinal_position", (table,))
        return list(db)
    
    def table_rows(self, dbname, table):
        db = CortezConn()
        db.execute("USE ["+dbname+"]", ())
        db.execute("SELECT * FROM ["+table+"]", ())
        return list(db)
    
    def row_count(self, dbname, table):
        db = CortezConn()
        db.execute("USE ["+dbname+"]", ())
        db.execute("SELECT count(*) FROM ["+table+"]", ())
        return db.fetchone()[0]
    
    def tables(self, dbname):
        db = CortezConn()
        db.execute("USE ["+dbname+"]", ())
        db.execute("SELECT name FROM sysobjects WHERE xtype='U'", ())
        return [n for n, in db]
    
    def databases(self):
        db = CortezConn()
        db.execute("SELECT name FROM master..sysdatabases", ())
        return [n for n, in db]
    
    def print_schema(self):
        for d in self.databases():
            if d in ('model', 'personnel', 'news', 'csguest', 'search', 'space', 'chingtai', 'CE8'):
                # no access
                continue
            if d in ('master', 'msdb', 'pubs', 'faculty'):
                # can't introspect for some reason
                continue
            print
            print "-" * 100
            for t in self.tables(d):
                if (d,t) in (('grad','10_ALL_INFO_FIX'), ('grad', 'PCSdownload'), ('grad', '0_EXCEL_FILE_FIX'),
                             ('grad', '1_BASIC_INFO_FIX'), ('grad', '2_EMERGENCY_INFO_FIX'), ('grad', '3_EDUCATION_INFO_FIX'),
                             ('grad', '4_REFERENCES_INFO_FIX'), ('grad', '5_TEST_INFO_FIX'), ('grad', '6_LANGUAGES_INFO_FIX'),
                             ('grad', '7_AWARDS_INFO_FIX'), ('grad', '8_RESEARCH_INFO_FIX'), ('grad', '9_EMPLOYMENT_FILE_FIX'),
                             ('grad', 'PCS_Identifier'), ('ra', 'deletedContract'), ('expenses', 'Advances')):
                    continue
                rows = self.row_count(d, t)
                print "%s.dbo.%s (%i)" % (d, t, rows)
                for name, typ, null, size in self.table_columns(d, t):
                    print "  %s %s(%s) (null %s)" % (name, typ, size, null.strip())
                print

class GradImport(object):
    IMPORT_USER = 'csilop'
    GENDER_MAP = {
                  'male': 'M',
                  'female': 'F',
                  'unknow': 'U',
                  }
    # names of the requirements, in the same order they appear in the query in process_grad
    REQ_LIST = ('Supervisory Committee', 'Breadth Program Approved', 'Breadth Requirements', 'Courses Completed',
                'Depth Exam', 'CMPT 891', 'Thesis Proposal', 'Thesis Defence', 'Research Topic')
    REQ_OBSOLETE = ('CMPT 891',) # can be hidden
    BOOL_LOOKUP = {'yes': True, 'no': False}

    # cortez -> coursys status values
    STATUS_MAP = {
                  'Incomplete': 'INCO',
                  'Partial': 'INCO',
                  'Expired': 'EXPI',
                  'Complete': 'COMP',
                  'Received': 'COMP',
                  'Hold(-)': 'HOLD',
                  'Hold(+)': 'HOLD',
                  'To Be Offered': 'OFFO',
                  'OfferOut': 'OFFO',
                  'Confirmed': 'CONF',
                  'Refused': 'REJE', # note: not realy used, so collapsed into other category
                  'Canceled': 'CANC',
                  'DeclinedOffer': 'DECL',
                  'expired': 'DECL',
                  'Rejected': 'REJE',
                  'Arrived': 'ARIV',
                  'Active': 'ACTI',
                  'PartTime': 'PART',
                  'OnLeave': 'LEAV',
                  'Graduated': 'GRAD',
                  'Withdrawn': 'WIDR',
                  'ArchiveSP': 'ARSP',
                  }

    LETTER_TEMPLATES = [
            ("MSc Invite", "Dear {{ title }} {{ last_name }},\r\n\r\nOn behalf of the School of Computing Science I am pleased to inform you that you have been admitted to the {{ program }} starting in {{ start_semester }}.\r\n\r\nWe are able to provide you with financial support in the amount of $XXXX per year for YYY year(s) in the program, given you make satisfactory progress, from a variety of sources including: research and teaching assistant appointments, graduate fellowships, scholarships, etc. Should you receive an external award (i.e. NSERC) to support your studies, financial support from SFU will instead consist of a minimum $6,000 \"top up\" per annum while you hold the external award, again from a variety of sources. You can expect continued funding for subsequent years, again subject to satisfactory progress and performance in course work and research towards the completion of your degree, and the continued availability of funding.\r\n\r\nSFU has 3 semesters per year. The typical time to complete the MSc program is 6 to 8 semesters and 12 to 15 semesters for the PhD program.\r\n\r\nIf you are an International Student you are required to obtain a study permit from Immigration Canada before you can begin your program. You will need to provide Immigration officials with an official letter of admission and a letter of acceptance which are both issued by the Dean of Graduate Studies offices. Please contact the nearest Canadian Embassy or High Commission for information on obtaining a study permit.\r\n\r\nPlease note that the financial support that we offer does not include tuition fees. The current tuition fees for Simon Fraser University graduate students are $1,661.60 per semester (or $4,984.80 per year), although it is possible there may be an increase in the Fall {{ start_year }}.\r\n\r\nDr. {{ supervisor_name }} has been assigned to be your potential supervisor and we ask that you please contact  {{ supervisor_himher }}  prior to your arrival at SFU. Dr. {{ supervisor_name }} may remain your permanent supervisor, but if this is not the case, {{ supervisor_heshe }} and I will work with you to choose a different supervisor who is more suited to your interests and needs.\r\n\r\nPlease reply as soon as possible whether you intend to register in the Spring 2013 semester and accept the financial support offered. This offer is valid until October 18th, 2012. If you have any questions or require clarification, please feel free to contact Dr. {{ supervisor_name }} ({{ supervisor_email }}) or our Graduate Program Assistant, Val Galat (csgrada@sfu.ca).\r\n\r\nWe are looking forward to your arrival and if you require assistance, please do not hesitate to contact us.", False),
            ("PhD Invite", "Dear {{ title }} {{ last_name }},\r\n\r\nOn behalf of the School of Computing Science I am pleased to inform you that you have been admitted to the {{ program }} starting in {{ start_semester }}.\r\n\r\nWe are able to provide you with financial support in the amount of {{ promise }} per year for YYY year(s) in the program, given you make satisfactory progress, from a variety of sources including: research and teaching assistant appointments, graduate fellowships, scholarships, etc. Should you receive an external award (i.e. NSERC) to support your studies, financial support from SFU will instead consist of a minimum $6,000 \"top up\" per annum while you hold the external award, again from a variety of sources. You can expect continued funding for subsequent years, again subject to satisfactory progress and performance in course work and research towards the completion of your degree, and the continued availability of funding.\r\n\r\nSFU has 3 semesters per year. The typical time to complete the MSc program is 6 to 8 semesters and 12 to 15 semesters for the PhD program.\r\n\r\nIf you are an International Student you are required to obtain a study permit from Immigration Canada before you can begin your program. You will need to provide Immigration officials with an official letter of admission and a letter of acceptance which are both issued by the Dean of Graduate Studies offices. Please contact the nearest Canadian Embassy or High Commission for information on obtaining a study permit.\r\n\r\nPlease note that the financial support that we offer does not include tuition fees. The current tuition fees for Simon Fraser University graduate students are $1,661.60 per semester (or $4,984.80 per year), although it is possible there may be an increase in the Fall {{ start_year }}.\r\n\r\nDr. {{ supervisor_name }} has been assigned to be your potential supervisor and we ask that you please contact {{ supervisor_himher }} prior to your arrival at SFU. Dr. {{ supervisor_name }} may remain your permanent supervisor, but if this is not the case, {{ supervisor_heshe }} and I will work with you to choose a different supervisor who is more suited to your interests and needs.\r\n\r\nPlease reply as soon as possible whether you intend to register in the Spring 2013 semester and accept the financial support offered. This offer is valid until October 18th, 2012. If you have any questions or require clarification, please feel free to contact Dr. {{ supervisor_name }} ({{ supervisor_email }}) or our Graduate Program Assistant, Val Galat (csgrada@sfu.ca).\r\n\r\nWe are looking forward to your arrival and if you require assistance, please do not hesitate to contact us.", False),
            ("Special Student invite", "Dear {{ title }} {{ last_name }},\r\n\r\nOn behalf of the School of Computing Science, I am pleased to inform you that we have recommended to the University's Senate Graduate Studies Committee that you be admitted as a SPECIAL student in the {{ start_semester }} semester, to complete the following course:\r\n\r\nCMPT XXX\r\n\r\nAs a Special Student you are not taking courses toward a degree, diploma or certificate at this institution but merely taking courses for your professional development. This admission is for the {{ start_semester }} semester only. You must reapply for each semester you want to study. Please do so in a timely manner.\r\n\r\nYour application has been forwarded to the Senate Graduate Studies Committee for final approval. Upon approval of admission from them kindly confirm as soon as possible if you intend to be registered for the {{ start_semester }} semester. On arrival, make arrangements with the Graduate Program assistant to register you in your classes.\r\n", False),
            ("Visa letter", "This is to confirm that {{ title }} {{ first_name }} {{ last_name }} is currently a full-time graduate student in the School of Computing Science at Simon Fraser University in a program of studies leading to the {{ program }} degree.\r\n\r\n{{ title }} {{ last_name }} is currently employed in the School of Computing Science, as a {{ recent_empl }}. This employment is an integral part of {{ his_her }} course studies towards the {{ program }} degree. Simon Fraser University operates on a trimester system, so that students are supported for three semesters each year, providing a yearly support level of {{ promise }}. The tuition fee at Simon Fraser University at graduate school is $4,984.80 per year.\r\n\r\n{{ title }} {{ last_name }} is making satisfactory progress towards the completion of {{ his_her }} degree and can expect to receive income at this rate in the future until the degree program has been completed. {{ title }} {{ last_name }} is expected to complete {{ his_her }} studies by MONTH_AND_YEAR.", False),
            ("International letter", "This is to advise you that {{ title }} {{ first_name }} {{ last_name }}, a {{ program }} Student in the School of Computing Science, has been employed as follows.\r\n\r\n{% if tafunding %}Teaching assistant responsibilities include providing tutorials, office hours and marking assignments. {{title}} {{last_name}}'s assignments have been:\r\n\r\n{{ tafunding }}{% endif %}\r\n{% if rafunding %}Research assistants assist/provide research services to faculty. {{title}} {{last_name}}'s assignments have been:\r\n\r\n{{ rafunding }}{% endif %}\r\n{% if scholarships %}{{title}} {{last_name}} has received the following scholarships:\r\n\r\n{{ scholarships }}{% endif %}\r\n\r\n{{ title }} {{ last_name }} is making satisfactory progress toward the completion of {{ his_her }} degree and is expected to complete {{ his_her }} studies by MONTH YEAR.\r\n", False),
            ("Cortez Import", '', True),
            ]
    GRAD_FLAGS = ['GDDP', 'Co-op']
    LETTER_TYPE_MAP = {
            'OFR_PHD': "PhD Invite",
            'OFR_MSC': "MSc Invite",
            'OFR_QUALIFYING': 'Special Student invite',
            'OFR_SPECIAL': 'Special Student invite',
            }
    COMMENT_TYPE_MAP = {
            'Schol': 'SCO',
            'Other': 'OTH',
            'None': 'OTH',
            'RA': 'RA',
            'TA': 'TA',
            }

    def __init__(self):
        self.db = CortezConn()
        self.db.execute("USE [grad]", ())
        self.cs_setup()
        self.get_ra_map()
        self.make_future_semesters()
    
    def get_ra_map(self):
        self.ra_map = {}
        for appt in RAAppointment.objects.filter(unit=self.unit):
            if 'cortezid' in appt.config:
                self.ra_map[appt.config['cortezid']] = appt
    
    def make_semester(self, name, start, end):
        s = Semester(name=name, start=start, end=end)
        s.save()
        
        first_monday = start
        while first_monday.weekday() != 0:
            first_monday += datetime.timedelta(days=1)    
        wk = SemesterWeek(semester=s, week=1, monday=first_monday)
        wk.save()

    def make_future_semesters(self):
        """
        There are some scholarships far in the future: must create those Semester objects manually.
        """
        try: Semester.objects.get(name='1141')
        except Semester.DoesNotExist: self.make_semester('1141', datetime.date(2014,1,1), datetime.date(2014,4,30))
        try: Semester.objects.get(name='1144')
        except Semester.DoesNotExist: self.make_semester('1144', datetime.date(2014,5,1), datetime.date(2014,8,30))
        try: Semester.objects.get(name='1147')
        except Semester.DoesNotExist: self.make_semester('1147', datetime.date(2014,9,1), datetime.date(2014,12,30))
        try: Semester.objects.get(name='1151')
        except Semester.DoesNotExist: self.make_semester('1151', datetime.date(2015,1,1), datetime.date(2015,4,30))
        try: Semester.objects.get(name='1154')
        except Semester.DoesNotExist: self.make_semester('1154', datetime.date(2015,5,1), datetime.date(2015,8,30))
        try: Semester.objects.get(name='1157')
        except Semester.DoesNotExist: self.make_semester('1157', datetime.date(2015,9,1), datetime.date(2015,12,30))
        try: Semester.objects.get(name='1161')
        except Semester.DoesNotExist: self.make_semester('1161', datetime.date(2016,1,1), datetime.date(2016,4,30))
        try: Semester.objects.get(name='1164')
        except Semester.DoesNotExist: self.make_semester('1164', datetime.date(2016,5,1), datetime.date(2016,8,30))
        try: Semester.objects.get(name='1167')
        except Semester.DoesNotExist: self.make_semester('1167', datetime.date(2016,9,1), datetime.date(2016,12,30))
        try: Semester.objects.get(name='1171')
        except Semester.DoesNotExist: self.make_semester('1171', datetime.date(2017,1,1), datetime.date(2017,4,30))
        try: Semester.objects.get(name='1174')
        except Semester.DoesNotExist: self.make_semester('1174', datetime.date(2017,5,1), datetime.date(2017,8,30))
        try: Semester.objects.get(name='1177')
        except Semester.DoesNotExist: self.make_semester('1177', datetime.date(2017,9,1), datetime.date(2017,12,30))
        
        
        
    def cs_setup(self):
        print "Setting up CMPT grad programs..."
        cmpt = Unit.objects.get(slug='cmpt')
        self.unit = cmpt
        programs = [('MSc Thesis', 'MSc Thesis option'), ('MSc Proj', 'MSc Project option'),
                    ('MSc Course', 'MSc Course option'), ('PhD', 'PhD'),
                    ('Special', 'Special Arrangements'), ('Qualifying', 'Qualifying Student')]
        for lbl, dsc in programs:
            gp, new_gp = GradProgram.objects.get_or_create(unit=cmpt, label=lbl)
            if new_gp:
                gp.description = dsc
                gp.created_by = self.IMPORT_USER
                gp.modified_by = self.IMPORT_USER
                gp.save()

        self.PROGRAM_MAP = {
                   ('MSc', 'Thesis'): GradProgram.objects.get(unit__slug='cmpt', slug='mscthesis'),
                   ('MSc', 'Project'): GradProgram.objects.get(unit__slug='cmpt', slug='mscproj'),
                   ('MSc', 'Course'): GradProgram.objects.get(unit__slug='cmpt', slug='msccourse'),
                   ('MSc', ''): GradProgram.objects.get(unit__slug='cmpt', slug='mscthesis'), # ???
                   ('MSc', None): GradProgram.objects.get(unit__slug='cmpt', slug='mscthesis'), # ???
                   ('PhD', 'Thesis'): GradProgram.objects.get(unit__slug='cmpt', slug='phd'),
                   ('PhD', ''): GradProgram.objects.get(unit__slug='cmpt', slug='phd'),
                   ('PhD', None): GradProgram.objects.get(unit__slug='cmpt', slug='phd'),
                   ('Special', 'Thesis'): GradProgram.objects.get(unit__slug='cmpt', slug='special'),
                   ('Special', ''): GradProgram.objects.get(unit__slug='cmpt', slug='special'),
                   ('Special', None): GradProgram.objects.get(unit__slug='cmpt', slug='special'),
                   ('Qualifying', 'Thesis'): GradProgram.objects.get(unit__slug='cmpt', slug='qualifying'),
                   ('Qualifying', ''): GradProgram.objects.get(unit__slug='cmpt', slug='qualifying'),
                   }
        
        for label, content, hidden in self.LETTER_TEMPLATES:
            try:
                template = LetterTemplate.objects.get(unit=cmpt, label=label)
            except LetterTemplate.DoesNotExist:
                template = LetterTemplate(unit=cmpt, label=label, content=content, created_by='csilop', hidden=hidden)
                template.save()
            
            if hidden:
                # the hidden one is the default for imports
                self.template = template  
        
        for label in self.GRAD_FLAGS:
            try:
                GradFlag.objects.get(unit=cmpt, label=label)
            except GradFlag.DoesNotExist:
                GradFlag(unit=cmpt, label=label).save()
        

    def get_semester_for_date(self, date):
        # guess relevant semester for given date
        s = Semester.objects.filter(start__lte=date+datetime.timedelta(days=10)).order_by('-start')[0]
        diff = date.date()-s.start
        if diff > datetime.timedelta(days=140):
            raise ValueError, "Couldn't find semester for %s." % (date)
        return s
            
    def find_supervisor(self, supname):
        """
        Find supervisor by userid if possible; return (Person, external).
        """
        if supname == 'mitchell':
            supname = 'dgm'
        person = get_person_by_userid(supname)
        if person:
            external = None
        elif supname.islower() and len(supname) <= 8:
            # seems to be a userid that we can't find: store as best possible
            person = None
            external = supname + '@sfu.ca'
        else:
            person = None
            external = supname
        
        return person, external

    @transaction.commit_on_success
    def process_grad(self, cortezid, sin, emplid, email, birthdate, gender,
                     english, mothertoungue, canadian, passport, visa, currentstatus, lastmod):
        """
        Process one imported student
        
        Argument list must be in the same order at the query in get_students below.
        """
        
        # TODO: should use get_person_grad for production run (but add_person is good enough for testing)
        p = get_person_grad(emplid, commit=True)
        #p = add_person(emplid, get_userid=False)
        if p is None:
            # ignore emplid-less grads?
            # all seem to start in 2006/2007 and have no end date
            return
        
        #print emplid, p
        # fill in the Person object with Cortez data
        if email:
            if '@' in email: # seem to have some userids in there too: let those stay in Person.userid
                p.config['applic_email'] = email
        if birthdate:
            p.config['birthdate'] = birthdate.date().isoformat()
        p.config['gender'] = self.GENDER_MAP[gender.lower()]
        p.config['cortezid'] = cortezid
        
        p.save()
                        
        self.db.execute("SELECT Program, Degreetype, SemesterStarted, SemesterFinished, "
                        "SupComSelected, BreProApproved, BreReqCompleted, CourseReqCompleted, "
                        "DepExamCompleted, CMPT891Completed, ThProApproved, ThDefended, ReaTopicChosen, "
                        "Supervisor1, Supervisor2, Supervisor3, Supervisor4, CoSupervisor, Sponsor, ResearchArea "
                        "FROM AcademicRecord WHERE Identifier=%s", (cortezid,))

        for prog, progtype, sem_start, sem_finish, supcom, brepro, brereq, crscom, depexam, \
                cmpt891, thepro, thedef, reatop, sup1,sup2,sup3,sup4, cosup, sponsor, research in list(self.db):
            try: sem_start = get_or_create_semester(sem_start)
            except ValueError: sem_start = None
            try: sem_finish = get_or_create_semester(sem_finish)
            except ValueError: sem_finish = None

            # get/create the GradStudent object
            if prog is None:
                # no program => don't import
                # all seem to start in 2006/2007 and have no end date
                continue

            prog = self.PROGRAM_MAP[(prog, progtype)]

            gs, new_gs = GradStudent.objects.get_or_create(person=p, program=prog)
            if new_gs:
                gs.created_by = self.IMPORT_USER
            gs.modified_by = self.IMPORT_USER
            gs.save()

            # basic info
            gs.mother_tongue = mothertoungue
            gs.is_canadian = self.BOOL_LOOKUP[canadian.lower()]
            gs.passport_issued_by = passport
            if sin:
                gs.config['sin'] = sin
            if research:
                gs.research_area = research
            
            # fill in their completed requirements
            # order of reqs must match self.REQ_LIST
            reqs = (supcom, brepro, brereq, crscom, depexam, cmpt891, thepro, thedef, reatop)
            for completed, req_name in zip(reqs, self.REQ_LIST):
                if not completed or completed.lower() in ('not taken',):
                    continue
                hiddenreq = req_name in self.REQ_OBSOLETE
                req, _ = GradRequirement.objects.get_or_create(program=prog, description=req_name, hidden=hiddenreq)
                try:
                    cr = CompletedRequirement.objects.get(requirement=req, student=gs)
                    new_cr = False
                except CompletedRequirement.DoesNotExist:
                    cr = CompletedRequirement(requirement=req, student=gs)
                    new_cr = True

                if new_cr: # if it was already there, don't bother fiddling with it
                    if completed.lower() == 'passed':
                        sem = sem_finish or sem_start
                        notes = 'No semester on cortez: used finishing semester.'
                    elif completed.lower() == 'waived':
                        sem = sem_finish or sem_start
                        notes = 'Waived'                        
                    else:
                        notes = None
                        try:
                            sem = get_or_create_semester(completed)
                        except ValueError:
                            sem = get_or_create_semester('0'+completed)
                    
                    cr.semester = sem
                    cr.notes = notes
                    cr.save()
            
            # Supervisors
            for pos, supname in zip(range(1,5), [sup1,sup2,sup3,sup4]):
                if not supname: continue
                person, external = self.find_supervisor(supname)
                is_senior = pos==1 or (pos==2 and cosup)
                suptype = "SEN" if is_senior else "COM"
                
                sups = Supervisor.objects.filter(student=gs, supervisor=person, external=external, supervisor_type=suptype)
                if sups:
                    sup = sups[0]
                else:
                    sup = Supervisor(student=gs, supervisor=person, external=external, supervisor_type=suptype)
                    sup.created_by = self.IMPORT_USER
                sup.modified_by = self.IMPORT_USER
                sup.save()
                    
            
            # Examining Committee
            self.db.execute("SELECT Chair, ExtName, ExtDep, ExtInst, ExtEmail, SFUExaminer "
                            "FROM ExamCommittee WHERE Identifier=%s", (cortezid,))
            for chair, extname, extdep, extinst, extemail, sfuexam in self.db:
                if chair:
                    person, external = self.find_supervisor(chair)
                    sups = Supervisor.objects.filter(student=gs, supervisor=person, external=external, supervisor_type='CHA')
                    if sups:
                        sup = sups[0]
                    else:
                        sup = Supervisor(student=gs, supervisor=person, external=external, supervisor_type='CHA')
                        sup.created_by = self.IMPORT_USER
                    sup.removed = False
                    sup.modified_by = self.IMPORT_USER
                    sup.save()
                
                if sfuexam:
                    person, external = self.find_supervisor(sfuexam)
                    sups = Supervisor.objects.filter(student=gs, supervisor=person, external=external, supervisor_type='SFU')
                    if sups:
                        sup = sups[0]
                    else:
                        sup = Supervisor(student=gs, supervisor=person, external=external, supervisor_type='SFU')
                        sup.created_by = self.IMPORT_USER
                    if extemail:
                        sup.set_email(extemail)
                    
                    sup.removed = False
                    sup.modified_by = self.IMPORT_USER
                    sup.save()
                
                if extname:
                    if extname=='mitchell':
                        extname='dgm'
                    external = extname
                    if extdep:
                        external += ", " + extdep
                    if extinst:
                        external += ", " + extinst
                    sups = Supervisor.objects.filter(student=gs, supervisor=None, external=external, supervisor_type='EXT')
                    if sups:
                        sup = sups[0]
                    else:
                        sup = Supervisor(student=gs, supervisor=None, external=external, supervisor_type='EXT')
                        sup.created_by = self.IMPORT_USER
                    sup.removed = False
                    sup.modified_by = self.IMPORT_USER
                    sup.save()


            # potential supervisor
            if sponsor and sponsor not in ['-None-', '-Office-']:
                person, external = self.find_supervisor(sponsor)
                
                sups = Supervisor.objects.filter(student=gs, supervisor=person, external=external, supervisor_type='POT')
                if sups:
                    sup = sups[0]
                else:
                    sup = Supervisor(student=gs, supervisor=person, external=external, supervisor_type='POT')
                    sup.created_by = self.IMPORT_USER
                sup.removed = False
                sup.modified_by = self.IMPORT_USER
                sup.save()


            # Status and application status
            self.db.execute("SELECT Status, Date, AsOfSem "
                        "FROM Status WHERE Identifier=%s "
                        "ORDER BY Date", (cortezid,))
            for status, date, semname in self.db:
                if semname:
                    sem = Semester.objects.get(name=semname)
                else:
                    sem = self.get_semester_for_date(date)

                # create/update GradStatus
                sts = GradStatus.objects.filter(student=gs, status=self.STATUS_MAP[status], start=sem)
                if sts:
                    st = sts[0]
                else:
                    st = GradStatus(student=gs, status=self.STATUS_MAP[status], start=sem, end=None)
                
                if date:
                    st.start_date = date.date()
                st.save(close_others=True)
            
            gs.save()
            
            # check that the cortez current status is the one we're displaying/using
            curr_st = self.STATUS_MAP[currentstatus]
            if gs.current_status != curr_st:
                # the current status wasn't found: add one last GradStatus to represent it
                st = GradStatus(student=gs, status=curr_st, start=Semester.get_semester(lastmod), start_date=lastmod)
                st.save()
            
            # make sure final status is left open
            st = GradStatus.objects.filter(student=gs, status=curr_st).order_by('-start')[0]
            if st.end:
                st.end = None
                st.save()

            # letters
            self.db.execute("SELECT LetterType, Modifier, Content, Date from LetterArchive where Identifier=%s", (cortezid,))
            for lt, modifier, content, datetime in self.db:
                template = LetterTemplate.objects.filter(unit=self.unit, label=self.LETTER_TYPE_MAP[lt])[0]
                content = content.replace('$PAGEBREAK$', '')
                date = datetime.date()
                letters = Letter.objects.filter(student=gs, date=date)
                if letters:
                    letter = letters[0]
                else:
                    letter = Letter(student=gs, date=date)
                
                letter.created_by = modifier.lower()
                letter.content = content
                letter.template = template
                letter.to_lines = ''
                letter.from_person = None
                letter.created_at = datetime
                letter.save()
            
            # financial promises
            self.db.execute("SELECT startsemester, endsemester, amount, comment FROM FSPromises where Identifier=%s", (cortezid,))
            for startsemester, endsemester, amount, comment in self.db:
                start = get_or_create_semester(startsemester)
                end = get_or_create_semester(endsemester)
                promises = Promise.objects.filter(student=gs, start_semester=start, end_semester=end)
                if promises:
                    p = promises[0]
                else:
                    p = Promise(student=gs, start_semester=start, end_semester=end)
                
                p.amount = amount
                p.comment = comment
                p.save()
            
            # scholarships
            self.db.execute("SELECT Name, Amount, DateRec, DateExp, RAShip_ID, External FROM Scholarships where Identifier=%s", (cortezid,))
            for name, amount, startsem, endsem, raship, external in self.db:
                startsem = get_or_create_semester(startsem)
                endsem = get_or_create_semester(endsem)
                try:
                    scholtype = ScholarshipType.objects.get(unit=self.unit, name=name, eligible=(not external))
                except ScholarshipType.DoesNotExist:
                    scholtype = ScholarshipType(unit=self.unit, name=name, eligible=(not external))
                    scholtype.comments = "Imported from Cortez"
                    scholtype.save()
                
                schols = Scholarship.objects.filter(scholarship_type=scholtype, student=gs, start_semester=startsem, end_semester=endsem)
                if schols:
                    schol = schols[0]
                else:
                    schol = Scholarship(scholarship_type=scholtype, student=gs, start_semester=startsem, end_semester=endsem)
                
                amount = amount.replace(',','')
                schol.amount = decimal.Decimal(amount)
                schol.save()
                if raship:
                    ra = self.ra_map[raship]
                    ra.scholarship = schol
                    ra.save()
            
            # other funding
            self.db.execute("SELECT Semester, OtherAmount, OtherType, Comments, isTravel FROM FinancialSupport where Identifier=%s and OtherAmount is not null", (cortezid,))
            for sem, amt, othertype, comments, _ in self.db:
                if not amt:
                    continue
                semester = Semester.objects.get(name=sem)
                if amt[0] == '$':
                    amt = amt[1:]
                amount = decimal.Decimal(amt)
                ofs = OtherFunding.objects.filter(student=gs, semester=semester, description=othertype)
                if ofs:
                    of = ofs[0]
                else:
                    of = OtherFunding(student=gs, semester=semester, description=othertype)
                
                of.amount = amount
                of.eligible = True
                of.comments = comments
                of.save()
        
            # program changes
            self.db.execute("SELECT program, degreetype, date, asofsem "
                            "FROM Programs WHERE Identifier=%s", (cortezid,))
            count = 0
            for program, degreetype, date, semname in self.db:
                progr = self.PROGRAM_MAP[(program, degreetype)]
                sem = Semester.objects.get(name=semname)
                count += 1
                phs = GradProgramHistory.objects.filter(student=gs, program=progr, start_semester=sem)
                if phs:
                    ph = phs[0]
                else:
                    ph = GradProgramHistory(student=gs, program=progr, start_semester=sem)
                ph.starting = date
                ph.save()
        
            if count == 0:
                # no program history for this student
                stsem = gs.start_semester
                if not stsem:
                    st = GradStatus.objects.filter(student=gs).order_by('-start__name')[0]
                    stsem = st.start
                ph = GradProgramHistory(student=gs, program=gs.program, start_semester=stsem)
                ph.save()
        
            # financial comments
            self.db.execute("SELECT semester, category, id, lastmodified, comment "
                            "FROM FinancialComments WHERE Identifier=%s", (cortezid,))
            for semname, category, userid, lastmod, comment in self.db:
                sem = Semester.objects.get(name=semname)
                cat = self.COMMENT_TYPE_MAP[category]
                lastmod = lastmod.replace(second=0, microsecond=0)
                userid = userid.lower()
                
                fcs = FinancialComment.objects.filter(student=gs, semester=sem, comment_type=cat, created_by=userid, created_at=lastmod)
                if not fcs:
                    fc = FinancialComment(student=gs, semester=sem, comment_type=cat, created_by=userid, created_at=lastmod)
                else:
                    fc = fcs[0]
                
                fc.comment=comment
                fc.save()


                

    
    def get_students(self):
        print "Importing grad students..."
        
        self.db.execute("SELECT pi.Identifier, pi.SIN, pi.StudentNumber, "
                        "pi.Email, pi.BirthDate, pi.Sex, pi.EnglishFluent, pi.MotherTongue, pi.Canadian, pi.Passport, "
                        "pi.Visa, pi.Status, pi.LastModified, pi.LastName FROM PersonalInfo pi "
                        "WHERE pi.StudentNumber not in (' ', 'na', 'N/A', 'NO', 'Not App.', 'N.A.', '-no-') " 
                        #"AND LastName>'M' "
                        "ORDER BY pi.LastName"
                        , ())
        initial = None
        for row in list(self.db):
            emplid = row[2]
            i = row[-1][0].upper()
            if i != initial:
                print "  ", i
                initial = i
                time.sleep(0.5)
            if not(emplid.isdigit() and len(emplid)==9):
                # TODO: what about them and the other no-emplid rows?
                continue
            try:
                self.process_grad(*(row[:-1]))
            except:
                print row
                raise

class TAImport(object):
    IMPORT_USER = 'csilop'
    UNIT = Unit.objects.get(slug='cmpt')
    TA_CONTACT = Person.objects.get(userid='ggbaker')
    
    CATEGORY_MAP = {
                    'mscc': 'GTA1',
                    'mscp': 'GTA1',
                    'msct': 'GTA1',
                    'msc': 'GTA1',
                    'phd': 'GTA2',
                    'bsc': 'UTA',
                    'ext': 'ETA',
                    '': 'ETA',
                    }
    INITIAL_MAP = {
                   'yes': 'INIT',
                   None: 'INIT',
                   'no': 'REAP',
                   }
    TA_DESC_MAP = {
                   'Office/Marking': 'Office/Marking',
                   'Office/Marking/Labs': 'Office/Marking/Labs',
                   'Development': 'Open Lab',
                   'CMPT': 'Open Lab',
                   'ARC': 'Open Lab',
                   'MTF': 'Open Lab',
                   'Course': 'Open Lab',
                   'course': 'Open Lab',
                   }
    CATEGORY_INDEX = {'GTA1': 0, 'GTA2': 1, 'UTA': 2, 'ETA': 3}
    # skill level words -> values
    SKILL_LEVEL_MAP = {
                       'None': 'NONE',
                       None: 'NONE',
                       '': 'NONE',
                       'Some': 'SOME',
                       'Expert': 'EXPR',
                       }
    CAMPUS_PREF_MAP = {
                       'B': ('BRNBY', 'PRF'),
                       'b': ('BRNBY', 'WIL'),
                       'S': ('SURRY', 'PRF'),
                       's': ('SURRY', 'WIL'),
                       'H': ('VANCR', 'PRF'),
                       'h': ('VANCR', 'WIL'),
                       }
    
    def __init__(self):
        self.db = CortezConn()
        self.db.execute("USE [esp]", ())

    def setup_accounts(self):
        accounts = [
                    ('00691', '11', 'MSc TA'),
                    ('06406', '11', 'PhD TA'),
                    ('06407', '11', 'External TA'),
                    ('99622', '11', 'Undergrad TA'),
                    ('00690', '11', 'Undergrad TA'),
                    ]
        self.positions = {}
        for p, a, desc in accounts:
            acc, _ = Account.objects.get_or_create(unit=self.UNIT, position_number=p, account_number=a)
            acc.title = desc
            acc.save()
            self.positions[p] = acc.id

    def make_fake_offering(self, sem, subject, number, section, title):
        """
        Create a fake offering for the old open lab/development courses
        """
        o = CourseOffering(subject=subject, number=number, section=section+'00', semester=Semester.objects.get(name=sem),
                           component='OPL', owner=self.UNIT, title=title, enrl_tot=0, enrl_cap=0, wait_tot=0)
        o.save()
        return o
    
    def get_offering_map(self):
        print "Mapping cortez offerings to CourSys..."
        # try the cache first: this is slow to build.
        try:
            fp = open("offering_map_cache.json", 'r')
            offeringid_map = json.load(fp)
            offeringid_map = dict([(k, CourseOffering.objects.get(id=offeringid_map[k])) for k in offeringid_map])
            self.offeringid_map = offeringid_map
            fp.close()
            print "Using cached offering map."
            return
        except IOError:
            pass
        
        offeringid_map = {}
        self.db.execute("SELECT Offering_ID, Semester_Description, Course_Number, Section, o.hasLabs, c.Course_Title "
                        "FROM Offerings o, Resource_Semesters s, Courses c "
                        "WHERE o.Semester_ID=s.Semester_ID AND o.Course_ID=c.Course_ID "
                        "ORDER BY Semester_Description", ())
        last = None
        for off_id, semname, crsnumber, section, hasLabs, title in self.db:
            if semname != last:
                print semname
                last = semname
            subject, number = crsnumber.split()
            
            # clean up ugliness from cortez
            if section[0]=='D' and number[0]>'4':
                section = 'G' + section[1:]

            if number.startswith('00'):
                number = 'XX' + number[2:]
            elif number.startswith('0'):
                number = 'X' + number[1:]
            elif number == '---':
                number = 'XXX'
            
            if semname in ['0987', '0991', '0994', '0997'] and subject=='CMPT' and number=='165':
                # was offered as CMPT 118 then
                number = '118'
            
            offerings = CourseOffering.objects.filter(subject=subject, number__startswith=number, semester__name=semname, section__startswith=section, section__endswith='00')
            if offerings.count() > 1:
                raise ValueError, "multiple offerings found: " + str(offerings)
            elif offerings:
                o = offerings[0]
                offeringid_map[off_id] = o
                o.set_labtas(bool(hasLabs))
                o.save()
            else:
                get_or_create_semester(semname)
                o = import_one_offering(semname, subject, number, section+'00')
                if o is None:
                    if number in ['XXX', 'XX0', 'XX2']:
                        o = self.make_fake_offering(semname, subject, number, section, title)        
                        offeringid_map[off_id] = o            
                    else:
                        print "Can't find:", semname, subject, number, section
                        continue
                else:
                    offeringid_map[off_id] = o
                    o.set_labtas(bool(hasLabs))
                    o.save()
            
            # get instructors if we can: might as well have better data
            #if o.crse_id and o.class_nbr:
            #    import_instructors(o)
        
        self.offeringid_map = offeringid_map
        
        # drop in a file to cache over multiple runs
        fp = open("offering_map_cache.json", 'w')
        # convert offerings to offering.ids for JSON
        offeringid_map = dict([(k, offeringid_map[k].id) for k in offeringid_map])
        json.dump(offeringid_map, fp, indent=1)
        fp.close()
    
    @transaction.commit_on_success
    def get_ta_postings(self):
        print "Getting TA semester postings..."
        self.setup_accounts()
        self.db.execute("SELECT currentYear, currentSem, applicationdeadline, acceptdeadline, "
                        "gta1amount, gta2amount, etaamount, utaamount, "
                        "gta1scholarship, gta2scholarship, etascholarship, utascholarship, "
                        "gta1positionNum, gta2positionNum, etapositionNum, utapositionNum, "
                        "asdday, asdmonth, asdYear, "
                        "aedday, aedmonth, aedYear "
                        "FROM tasearch.dbo.semesterinfo", ())
        for year, sem, applic, accept, gta1a,gta2a,etaa,utaa, gta1s,gta2s,etas,utas, gta1p,gta2p,etap,utap, asd,asm,asy, aed,aem,aey in self.db:
            strm = year+sem
            salary = [gta1a,gta2a,utaa,etaa]
            schol = [gta1s,gta2s,utas,etas]
            positions = [self.positions[p] for p in [gta1p,gta2p,utap,etap]]
            
            # fix broken data in system
            if asy < 1900: asy += 2000
            if aey < 1900: aey += 2000
            if strm in ['1107']:
                asm, asd = asd, asm
                aem, aed = aed, aem
            if strm in ['1111']:
                asm = 1

            start = datetime.date(asy, asm, asd)
            end = datetime.date(aey, aem, aed)
            try:
                applic = datetime.datetime.strptime(applic, "%B %d, %Y").date()
            except ValueError:
                applic = start
            try:
                accept = datetime.datetime.strptime(accept, "%B %d, %Y").date()
            except ValueError:
                accept = start

            semester = get_or_create_semester(strm)
            semester.save()
            postings = TAPosting.objects.filter(unit=self.UNIT, semester=semester)
            if postings:
                posting = postings[0]
            else:
                posting = TAPosting(unit=self.UNIT, semester=semester)

            posting.opens = applic
            posting.closes = applic
            posting.config['salary'] = salary
            posting.config['scholarship'] = schol
            posting.config['start'] = start
            posting.config['end'] = end
            posting.config['deadline'] = accept
            posting.config['contact'] = self.TA_CONTACT.id
            posting.config['accounts'] = positions
            posting.config['payperiods'] = "%.1f" % (self.total_seconds(end-start)/3600/24/14,) # close enough for the import
            posting.save()
    
    def create_fake_posting(self, sem):
        "Create totally fake posting object: better than throwing away corresponding TAs"
        print "  Faking TAPosting for %s" % (sem)
        posting = TAPosting(unit=self.UNIT, semester=sem)
        posting.opens = sem.start
        posting.closes = sem.start
        posting.config['start'] = sem.start
        posting.config['end'] = sem.end
        posting.config['deadline'] = sem.start
        posting.config['contact'] = self.TA_CONTACT.id
        posting.config['payperiods'] = "%.1f" % (self.total_seconds(sem.end-sem.start)/3600/24/14,) # close enough for the import
        posting.save()
        return posting
    
    def total_seconds(self, td):
        """
        Compensate for timedelta.total_seconds not existing in Python 2.6
        """
        tsec = (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6)/10**6
        return tsec
    
    @transaction.commit_on_success
    def get_ta(self, off_id, bu, salary, schol, description, payst, payen, posnum, initial, cond,
               tssu, remarks, app_bu, emplid, cat, sin, status, appid, campuspref):
        """
        Process one TA
        
        Argument order must be the same as query in get_tas below.
        """
        if bu == 0:
            return
        if not(emplid.isdigit() and len(emplid)==9):
            # there's only one, from 2004. Ignore.
            return
        if off_id not in self.offeringid_map:
            # Where did these offerings go? I'm troubled.
            print "missing offering_id:", off_id
            return
        offering = self.offeringid_map[off_id]
        p = get_person(emplid, commit=True)
        
        try:
            posting = TAPosting.objects.get(unit=self.UNIT, semester=offering.semester)
        except TAPosting.DoesNotExist:
            posting = self.create_fake_posting(offering.semester)
        
        # create TAApplication
        apps = TAApplication.objects.filter(posting=posting, person=p)
        if apps:
            app = apps[0]
        else:
            app = TAApplication(posting=posting, person=p)
        
        app.category = self.CATEGORY_MAP[cat]
        app.base_units = app_bu or 0
        app.sin = sin
        app.save()
        
        # create TAContract
        contracts = TAContract.objects.filter(posting=posting, application=app)
        if contracts:
            contract = contracts[0]
        else:
            contract = TAContract(posting=posting, application=app)

        try:
            pos_id = self.positions[posnum]
        except KeyError:
            pos_id = posting.config['accounts'][self.CATEGORY_INDEX[app.category]]
        
        contract.sin = sin
        contract.pay_start = payst or offering.semester.start
        contract.pay_end = payen or offering.semester.end
        contract.appt_category = app.category
        contract.position_number_id = pos_id
        contract.appt = self.INITIAL_MAP[initial]
        contract.pay_per_bu = "%.2f" % (salary/bu)
        contract.scholarship_per_bu = "%.2f" % (schol/bu)
        contract.deadline = posting.deadline()
        contract.appt_cond = bool(cond)
        contract.appt_tssu = bool(tssu)
        contract.status = 'SGN' if status=='accepted' else 'CAN'
        contract.remarks = remarks or ''
        contract.created_by = self.IMPORT_USER
        contract.save()
        
        # create TACourse
        crses = TACourse.objects.filter(course=offering, contract=contract)
        if crses:
            crs = crses[0]
        else:
            crs = TACourse(course=offering, contract=contract)
        
        d = self.TA_DESC_MAP[description.split()[0]]
        try:
            desc = CourseDescription.objects.get(unit=self.UNIT, description=d)
        except CourseDescription.DoesNotExist:
            desc = CourseDescription(unit=self.UNIT, description=d)
            if d == 'Office/Marking/Labs':
                desc.labtut = True
            desc.save()

        crs.description = desc
        crs.bu = bu
        crs.save()
        
        
        # course preferences
        self.db.execute("SELECT course, rank FROM tasearch.dbo.courseranking "
                        "WHERE appId=%s", (appid,))
        for course, rank in self.db:
            if course in ['-', '']:
                continue
            subj, num = course.split()
            courses = Course.objects.filter(subject=subj, number=num)
            if courses.count() > 1:
                raise "Multiple Courses found for %r" % (course)
            elif courses.count() == 1:
                crs = courses[0]
                CoursePreference.objects.get_or_create(app=app, course=crs, rank=rank+1)

        # skills
        self.db.execute("SELECT skillType, skillLevel FROM tasearch.dbo.taskills "
                        "WHERE appId=%s", (appid,))
        for skillname, level in self.db:
            if not skillname:
                continue
            skills = Skill.objects.filter(posting=posting, name=skillname)
            if skills:
                skill = skills[0]
            else:
                maxpos = Skill.objects.filter(posting=posting).aggregate(max_pos=Max('position'))['max_pos']
                if maxpos:
                    pos = maxpos+1
                else:
                    pos = 1
                skill = Skill(posting=posting, name=skillname, position=pos)
                skill.save()
            
            lvls = SkillLevel.objects.filter(skill=skill, app=app)
            if lvls:
                lvl = lvls[0]
            else:
                lvl = SkillLevel(skill=skill, app=app)
            
            lvl.level = self.SKILL_LEVEL_MAP[level]
            lvl.save()
        
        # campus preferences
        if campuspref:
            for c in campuspref:
                if c in self.CAMPUS_PREF_MAP:
                    campus, pref = self.CAMPUS_PREF_MAP[c]
                    cps = CampusPreference.objects.filter(app=app, campus=campus)
                    if cps:
                        cp = cps[0]
                    else:
                        cp = CampusPreference(app=app, campus=campus)
                    
                    if cp.pref != pref:
                        cp.pref = pref
                        cp.save()
                    
        

    
    def get_tas(self):  
        self.get_ta_postings()
        self.get_offering_map()
        print "Importing TAs..."
        
        #self.db.execute("SELECT studNum, appYear, appSemester, count(studNum) FROM tasearch.dbo.tainfo i "
        #                "group by studNum, appYear, appSemester having count(studNum)>1 order by appYear", ())
        #print list(self.db) 

        self.db.execute("SELECT o.Offering_ID, o.bu, o.salary, o.scholarship, o.description, "
                        "o.PayrollStartDate, o.PayrollEndDate, o.PositionNumber, o.initAppointment, "
                        "o.conditional, o.tssu, o.remarks, i.base, "
                        "i.studNum, i.category, i.socialInsurance, i.status, i.appId, i.campusPref, i.appYear "
                        "FROM TAOffering o, tasearch.dbo.tainfo i "
                        "WHERE o.TA_ID=i.appId ORDER BY i.appYear", ()) # and i.appYear>'109' 
        initial = None
        for row in list(self.db):
            i = row[-1]
            if i != initial:
                print "  ", 1900+int(i)
                initial = i
                time.sleep(0.5)

            self.get_ta(*row[:-1])


class RAImport(object):
    UNIT = Unit.objects.get(slug='cmpt')
    CATEGORY_MAP = {
                    'Scholarship': 'S',
                    }
    NAME_MAP = {
                    'Richard (Hao) Zhang': 'haoz',
                    'Mohamed Hefeeda (Surrey campus)': 'mhefeeda',
                    'Alexandra Fedorova': 'fedorova',
                    'Joe Peters': 'peters',
                    'David Mitchell': 'dgm',
                    'Daniel Weiskopf': 301038247,
                    'Arvind Gupta': 555002157,
                    'Amanda Woodhall': 'woodhama',
                    'David Fracchia': 555002200,
                    'Eugenia Ternovskaia': 'ter',
                    'Dirk Beyer': 301061499,
                    'Kay Wiese (Surrey campus)': 200112529,
                    'Department': 'tbruneau',
                    'JiaWei Han': 555002003,
                    'Kori Inkpen': 200028805,
                    'Qiang Yang': 555002793,
                    }
    FIXED_EMPLIDS = {
                     '20010417': '200110417',
                     '735605032': '200116277',
                     '500197419': '301020541',
                     '30109792': '301097926',
                     '30108973': '301089730',
                     '731385563': '913018430',
                     '30101368': '301013608',
                     '743394652': '301071447',
                     '97301125': '973011255',
                     '500197854': '301040373',
                     '30112745': '301127458',
                     }
    
    def __init__(self):
        self.db = CortezConn()
        self.db.execute("USE [ra]", ())
        noaccounts = Account.objects.filter(unit=self.UNIT, account_number=0, position_number=0) 
        if noaccounts:
            self.noaccount = noaccounts[0]
        else:
            self.noaccount = Account(unit=self.UNIT, account_number=0, position_number=0, title='Unknown', hidden=True)
            self.noaccount.save()
    
    @transaction.commit_on_success
    def get_ra(self, contractnumber, fund, project, position, reappt, startdate, enddate, category, faculty, msp, dental, \
            hourlyrate, biweeklyrate, biweeklyhours, biweeklyamount, lumpsumamount, lumpsumhours, payperiod, \
            totalamount, salarytype, notes, comments, emplid, sin):
        """
        Get one RA record. Argument list must match query in get_ras.
        """
        startdate = startdate.date()
        enddate = enddate.date()
        if not emplid or emplid in ['new', 'n/a']:
            # TODO: do what with them?
            return
        if emplid in self.FIXED_EMPLIDS:
            emplid = self.FIXED_EMPLIDS[emplid]
        
        p = add_person(emplid, commit=True, get_userid=False)
        if not p:
            print "No SIMS record found for RA %s" % (emplid)
            return

        ras = RAAppointment.objects.filter(unit=self.UNIT, person=p, start_date=startdate, end_date=enddate,
                                           config__contains=unicode(contractnumber))
        if ras:
            ra = ras[0]
        else:
            ra = RAAppointment(unit=self.UNIT, person=p, start_date=startdate, end_date=enddate)
        
        if 'cortezid' in ra.config and ra.config['cortezid'] != contractnumber:
            raise ValueError, "duplicate RAAppointment? %r %r" % (contractnumber, ra.config['cortezid'])
        ra.config['cortezid'] = contractnumber
        
        try:
            int(sin)
        except (ValueError, TypeError):
            sin = None
        ra.sin = sin if sin else 0
        
        if faculty in self.NAME_MAP:
            faculty_userid = self.NAME_MAP[faculty]
        else:
            self.db.execute("select StaffID from expenses.dbo.StaffLU where (FirstName+' '+LastName)=%s", (faculty,))
            row = self.db.fetchone()
            if row is None:
                raise ValueError, 'missing faculty name, ' + `faculty`
            faculty_userid = row[0]
            self.NAME_MAP[faculty] = faculty_userid
        
        #print ">>>", `faculty`, faculty_userid
        try:
            if isinstance(faculty_userid, basestring):
                ra.hiring_faculty = Person.objects.get(userid=faculty_userid)
            else:
                ra.hiring_faculty = Person.objects.get(emplid=faculty_userid)
        except Person.DoesNotExist:
            if isinstance(faculty_userid, basestring):
                ra.hiring_faculty = get_person_by_userid(faculty_userid)
            else:
                p = add_person(faculty_userid, commit=True, get_userid=False)
                ra.hiring_faculty = p

        try:
            int(project)
            int(fund)
        except ValueError:
            # TODO: handle missing fund/project somehow
            return
        proj, _ = Project.objects.get_or_create(unit=self.UNIT, project_number=int(project), fund_number=int(fund))
        ra.project = proj

        if biweeklyhours == '0.8.8':
            biweeklyhours = '8.8'
        elif biweeklyhours == '38.5.5.':
            biweeklyhours = '38.5'
        elif biweeklyhours.endswith('.'):
            biweeklyhours = biweeklyhours[:-1]

        biweeklyhours = decimal.Decimal(biweeklyhours)
        if salarytype == '0': # bi-weekly
            ra.pay_frequency = 'B'
            ra.lump_sum_pay = totalamount
            ra.biweekly_pay = biweeklyamount
            ra.pay_periods = payperiod
            ra.hourly_pay = hourlyrate
            ra.hours = biweeklyhours
            # TODO: hourly stuff usually missing: fix it somehow?
        elif salarytype == '2': # lump-sum
            ra.pay_frequency = 'L'
            ra.lump_sum_pay = lumpsumamount
            ra.biweekly_pay = lumpsumamount
            ra.pay_periods = 1
            ra.hourly_pay = lumpsumamount
            ra.hours = 1
        else:
            raise ValueError, str(salarytype)
        
        # account/position number
        if position:
            accts = Account.objects.filter(unit=self.UNIT, position_number=position)
            if accts:
                ra.account = accts[0]
            else:
                acct = Account(unit=self.UNIT, position_number=position, account_number=0, title=unicode(position))
                acct.save()
                ra.account = acct
        else:
            ra.account = self.noaccount
        
        ra.save()

    def get_ras(self):
        print "Importing RAs..."
        self.db.execute("SELECT c.ContractNumber, c.FundNumber, c.ProjectNumber, c.PositionNumber, c.ReAppointment, c.StartDate, c.EndDate, "
                        "c.HiringCategory, c.Faculty, c.MSP, c.DentalPlan, "
                        "c.HourlyEarningRate, c.BiweeklyEarningRate, c.BiweeklyHoursMin, c.BiweeklyAmount, "
                        "c.LumpSumAmount, c.LumpSumHours, c.PayPeriod, c.TotalAmount, c.SalaryType, "
                        "c.Notes, c.Comments, "
                        "r.StudentNumber, r.SIN, r.FamilyName "
                        "FROM Contract c LEFT JOIN RA r ON c.Identifier=r.Identifier "
                        #"WHERE r.FamilyName='Vinnik' "
                        "ORDER BY r.FamilyName", ())
        initial = None
        for row in list(self.db):
            if row[-1]:
                i = row[-1][0].upper()
                if i != initial:
                    print "  ", i
                    initial = i
                    time.sleep(0.5)

            self.get_ra(*row[:-1])
        #print all_types

if __name__ == '__main__':
    argv = sys.argv[1:]
    if '--schema' in argv:
        Introspection().print_schema()
        sys.exit(0)
    
    if '--userid' in argv:
        update_amaint_userids()
    if '--ta' in argv:
        TAImport().get_tas()
    if '--ra' in argv:
        RAImport().get_ras()
    if '--grad' in argv:
        GradImport().get_students()
