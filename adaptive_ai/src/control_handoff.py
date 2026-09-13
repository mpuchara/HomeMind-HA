from lease_journal import LeaseJournal


class ControlHandoff:
    def __init__(self, store):
        self.store = store
        self.journal = LeaseJournal(store)
