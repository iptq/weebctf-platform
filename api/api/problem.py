from flask import Blueprint, jsonify, session, request
from flask import current_app as app
from werkzeug import secure_filename

from models import db, Files, Problems, ProgrammingSubmissions, Solves, Teams, Users, Activity
from decorators import admins_only, api_wrapper, login_required, team_required, team_finalize_required, InternalException, WebException

import datetime
import hashlib
import imp
import json
import os
import random
import shutil
import markdown2

import autogen
import cache
import logger
import programming
import stats
import team
import user
import utils

blueprint = Blueprint("problem", __name__)

@blueprint.route("/add", methods=["POST"])
@api_wrapper
@admins_only
def problem_add():
	params = utils.flat_multi(request.form)
	title = params.get("title")
	category = params.get("category")
	description = params.get("description")
	hint = params.get("hint")
	value = params.get("value")
	grader_contents = params.get("grader_contents")
	bonus = params.get("bonus")
	autogen = params.get("autogen")
	try:
		add_problem(title, category, description, value, grader_contents, hint=hint, bonus=bonus, autogen=autogen)
	except Exception, e:
		raise WebException(str(e))
	return { "success": 1, "message": "Success!" }

@blueprint.route("/delete", methods=["POST"])
@admins_only
@api_wrapper
def problem_delete():
	params = utils.flat_multi(request.form)
	pid = params.get("pid")
	problem = Problems.query.filter_by(pid=pid).first()
	if problem:
		ProgrammingSubmissions.query.filter_by(pid=pid).delete()
		Solves.query.filter_by(pid=pid).delete()
		Activity.query.filter_by(pid=pid).delete()
		Problems.query.filter_by(pid=pid).delete()
		grader_folder = os.path.dirname(problem.grader)
		shutil.rmtree(grader_folder)
		db.session.commit()
		db.session.close()
		return { "success": 1, "message": "Success!" }
	raise WebException("Problem does not exist!")

@blueprint.route("/update", methods=["POST"])
@admins_only
@api_wrapper
def problem_update():
	params = utils.flat_multi(request.form)
	pid = params.get("pid")
	title = params.get("title")
	category = params.get("category")
	description = params.get("description")
	hint = params.get("hint")
	value = params.get("value")
	bonus = params.get("bonus")
	grader_contents = params.get("grader_contents")
	autogen = params.get("autogen")

	problem = Problems.query.filter_by(pid=pid).first()
	if problem:
		problem.title = title
		problem.category = category
		problem.description = description
		problem.hint = hint
		problem.value = value
		problem.bonus = bonus
		problem.autogen = autogen

		if category == "Programming":
			programming.validate_judge(grader_contents)
		else:
			validate_grader(grader_contents, autogen=int(autogen))

		with open(problem.grader, "w") as grader:
			grader.write(grader_contents)
			grader.close()

		db.session.add(problem)
		db.session.commit()
		db.session.close()

		return { "success": 1, "message": "Success!" }
	raise WebException("Problem does not exist!")

@blueprint.route("/submit", methods=["POST"])
@api_wrapper
@login_required
@team_required
@team_finalize_required
def problem_submit():
	params = utils.flat_multi(request.form)
	pid = params.get("pid")
	flag = params.get("flag")
	tid = session.get("tid")
	_user = user.get_user().first()
	username = _user.username

	problem = Problems.query.filter_by(pid=pid).first()
	_team = Teams.query.filter_by(tid=tid).first()
	solved = Solves.query.filter_by(pid=pid, tid=tid, correct=1).first()
	if solved:
		raise WebException("You already solved this problem.")

	flag_tried = Solves.query.filter_by(tid=tid, pid=pid, flag=flag).first()
	if flag_tried:
		raise WebException("Your team has already tried this solution.")

	if problem:
		old_rank, dummy = _team.place()
		dummy, dummy2, old_leader = stats.get_leaderboard()[0]
		if problem.category == "Programming":
			raise WebException("Please submit programming problems using the Programming interface.")
		grader = imp.load_source("grader", problem.grader)
		random = None
		if problem.autogen:
			random = autogen.get_random(pid, tid)
		correct, response = grader.grade(random, flag)

		solve = Solves(pid, _user.uid, tid, flag, correct)
		db.session.add(solve)
		db.session.commit()

		if correct:
			# Wait until after the solve has been added to the database before adding bonus
			solves = get_solves(pid)
			solve.bonus = [-1, solves][solves < 4]
			cache.invalidate_memoization(get_solves, pid)
			if _user:
				activity = Activity(_user.uid, 3, tid=tid, pid=pid)
				db.session.add(activity)
			db.session.commit()
			logger.log(__name__, "%s has solved %s by submitting %s" % (_team.teamname, problem.title, flag), level=logger.WARNING)

			new_rank, dummy = _team.place()
			if new_rank == 1 and old_rank > 1:
				activity = Activity(-1, 4, tid=_team.tid, pid=-1)
				db.session.add(activity)
				activity = Activity(-1, 5, tid=old_leader.tid, pid=-1)
				db.session.add(activity)
				db.session.commit()
				db.session.close()
			return { "success": 1, "message": response }
		else:
			logger.log(__name__, "%s has incorrectly submitted %s to %s" % (_team.teamname, flag, problem.title), level=logger.WARNING)
			raise WebException(response)

	else:
		raise WebException("Problem does not exist!")


@blueprint.route("/data", methods=["GET"])
@login_required
@api_wrapper
def problem_data():
	_user = user.get_user().first()
	if not user.is_admin():
		if not user.in_team(_user):
			raise WebException("You need a team.")
		if not team.team_finalized(team.get_team_of(_user.uid)):
			raise WebException("Your team is not finalized.")

	problems = Problems.query.order_by(Problems.value).all()
	problems_return = [ ]
	for problem in problems:
		solves = get_solves(problem.pid)
		solved = Solves.query.filter_by(pid=problem.pid, tid=session.get("tid", None), correct=1).first()
		solved = ["Solved", "Unsolved"][solved is None]
		description = process_description(problem.description)

		data = {
			"pid": problem.pid,
			"title": problem.title,
			"category": problem.category,
			"description": description,
			"hint": problem.hint,
			"value": problem.value,
			"solves": solves,
			"solved": solved
		}
		admin_data = {
			"description_source": problem.description,
			"threshold": problem.threshold,
			"weightmap": problem.weightmap,
			"grader_contents": open(problem.grader, "r").read(),
			"bonus": problem.bonus,
			"autogen": problem.autogen == True
		}
		if "admin" in session and session["admin"]:
			data.update(admin_data)
		if problem.autogen:
			grader = imp.load_source("grader", problem.grader)
			tid = session.get("tid", "team")
			try:
				data.update(grader.generate_problem(autogen.get_random(problem.pid, tid), problem.pid))
			except Exception, e:
				logger.log(__name__, "The grader for \"%s\" has thrown an error: %s" % (problem.title, e))
		problems_return.append(data)
	return { "success": 1, "problems": problems_return }

@blueprint.route("/solves", methods=["POST"])
@api_wrapper
@login_required
@team_required
@team_finalize_required
def problem_solves():
	params = utils.flat_multi(request.form)
	pid = params.get("pid")
	solves_return = []
	solves = Solves.query.filter_by(pid=pid, correct=True).order_by("date asc").all()
	for solve in solves:
		data = {
			"teamname": Teams.query.filter_by(tid=solve.tid).first().teamname,
			"date": utils.isoformat(float(solve.date))
		}
		solves_return.append(data)
	return { "success": 1, "solves": solves_return }

@blueprint.route("/clear_submissions", methods=["POST"])
@api_wrapper
@admins_only
def clear_solves():
	params = utils.flat_multi(request.form)

	pid = params.get("pid")
	Solves.query.filter_by(pid=pid).delete()
	ProgrammingSubmissions.query.filter_by(pid=pid).delete()
	cache.invalidate_memoization(get_solves, pid)
	db.session.commit()
	db.session.close()

	return { "success": 1, "message": "Submissions cleared." }

@cache.memoize(timeout=120)
def get_solves(pid):
	solves = Solves.query.filter_by(pid=pid, correct=1).count()
	return solves

def insert_problem(data, force=False):
	with app.app_context():
		if len(list(get_problem(pid=data["pid"]).all())) > 0:
			if force == True:
				_problem = Problems.query.filter_by(pid=data["pid"]).first()
				db.session.delete(_problem)
				db.session.commit()
			else:
				raise InternalException("Problem already exists.")
		insert = Problems(data["pid"], data["title"], data["category"], data["description"], data["value"])
		if "hint" in data: insert.hint = data["hint"]
		if "autogen" in data: insert.autogen = data["autogen"]
		if "bonus" in data: insert.bonus = data["bonus"]
		if "threshold" in data: insert.threshold = data["threshold"]
		if "weightmap" in data: insert.weightmap = data["weightmap"]
		db.session.add(insert)
		db.session.commit()
		db.session.close()
	return True

def get_problem(title=None, pid=None):
	match = {}
	if title != None:
		match.update({ "title": title })
	elif pid != None:
		match.update({ "pid": pid })
	with app.app_context():
		result = Problems.query.filter_by(**match)
		return result

@cache.memoize()
def num_problems():
	return Problems.query.filter_by().count()

def add_problem(title, category, description, value, grader_contents, pid=utils.generate_string(), hint="", bonus=0, autogen=0):
	grader_contents = str(grader_contents)
	pid = str(pid)
	value = int(value)

	title_exists = Problems.query.filter_by(title=title).first()
	if title_exists:
		raise WebException("Problem name already taken.")
	while Problems.query.filter_by(pid=pid).first():
		pid = utils.generate_string()
	if category == "Programming":
		programming.validate_judge(grader_contents)
	else:
		validate_grader(grader_contents, autogen=int(autogen))

	grader_folder = os.path.join(app.config["GRADER_FOLDER"], pid)
	if not os.path.exists(grader_folder):
		os.makedirs(grader_folder)
	grader_path = os.path.join(grader_folder, "grader.py")
	grader_file = open(grader_path, "w")
	grader_file.write(grader_contents)
	grader_file.close()

	problem = Problems(pid, title, category, description, value, grader_path, bonus=bonus, hint=hint, autogen=autogen)
	db.session.add(problem)

	files = request.files.getlist("files[]")
	for _file in files:
		filename = secure_filename(_file.filename)
		if len(filename) == 0:
			continue
		file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
		_file.save(file_path)
		db_file = Files(problem.pid, "/".join(file_path.split("/")[2:]))
		db.session.add(db_file)
	db.session.commit()
	db.session.close()

def process_description(description):
	description = markdown2.markdown(description)
	description = description.replace("href=\"", "target=\"_blank\" href=\"") # dirty hack
	return description

def validate_grader(grader_contents, autogen=False):
	tmp_grader = "/tmp/grader.py"

	open(tmp_grader, "w").write(grader_contents)

	try:
		grader = imp.load_source("grader", tmp_grader)
	except Exception, e:
		raise WebException("There is a syntax error in the grader: %s" % e)

	if autogen:
		# Autogenerated
		try:
			seed1 = utils.generate_string()
			seed2 = utils.generate_string()

			while seed1 == seed2:
				seed2 = utils.generate_string()

			random.seed(seed1)
			data = grader.generate_problem(random, "pid")
			assert type(data) == dict

			random.seed(seed1)
			flag1 = grader.generate_flag(random)
			random.seed(seed2)
			flag2 = grader.generate_flag(random)

			assert flag1 != flag2, "generate_flag() has generated the same flag twice."

			random.seed(seed1)
			correct, message = grader.grade(random, flag1)

			assert correct == True, "Grader marked correct flag as incorrect."
		except AssertionError, e:
			raise WebException(e)
		except Exception, e:
			raise WebException(e)
	else:
		try:
			correct, message = grader.grade(None, "hi")
			assert type(correct) == bool, "First return from grade() must be a boolean."
			assert hasattr(grader, "flag"), "Grader is missing the flag."

			correct, message = grader.grade(None, grader.flag)
			assert correct, "Grader marked correct flag as incorrect."
		except AssertionError, e:
			raise WebException(e)
		except Exception, e:
			raise WebException(e)
